from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.autosmm_manager import (
    AutoSmmConfig,
    AutoSmmLotRule,
    AutoSmmOrder,
    SmmApiClient,
    resolve_lot_rule, provider_id,
)
from playerokapi.enums import ItemDealStatuses

logger = logging.getLogger("plugin.autosmm")

PLUGIN_META = {
    "id": "autosmm",
    "name": "AutoSMM (Накрутка / SMM панель)",
    "version": "2.1.0",
    "author": "Playerok Bot",
    "description": (
        "Автоматическое создание заказов в SMM-панелях (smmway и любых сервисах со стандартным SMM API v2) "
        "при покупке лотов на Playerok. Удобный графический интерфейс привязки товаров, каталог услуг, "
        "привязки по фразе, история и статус заказов, ручные запросы refill при поддержке панели."
    ),
    "settings": {
        "auto_confirm_deal": {
            "type": "bool",
            "name": "Подтверждать сделку в SENT после заказа",
            "default": True,
            "description": "Автоматически завершать сделку на Playerok после успешного запуска накрутки",
        },
        "notify_tg": {
            "type": "bool",
            "name": "Уведомлять в TG о создании заказа",
            "default": True,
            "description": "Присылать отчет в Telegram об отправке накрутки",
        },
    },
}

_URL_RE = re.compile(r"https?://[^\s<>\"']+|@[a-zA-Z0-9_]{4,32}|t\.me/[a-zA-Z0-9_]{4,32}|vk\.com/[a-zA-Z0-9_.-]+")


def _extract_target_link(deal: Any) -> str | None:
    """Пытается извлечь целевую ссылку покупателя из сделки или данных полей."""
    fields = getattr(deal, "data_fields", []) or []
    for f in fields:
        val = getattr(f, "value", None) or (f.get("value") if isinstance(f, dict) else None)
        if val:
            match = _URL_RE.search(str(val))
            if match:
                return match.group(0)

    comment = getattr(deal, "comment", None) or getattr(deal, "instruction_for_seller", None)
    if comment:
        match = _URL_RE.search(str(comment))
        if match:
            return match.group(0)

    return None


async def on_deal(ctx: Any, deal: Any) -> None:
    """Обработчик новых сделок Playerok для автоматического заказа в SMM панели."""
    account = ctx.account
    item = getattr(deal, "item", None)
    item_id = str(getattr(item, "id", "") or "")
    deal_id = str(getattr(deal, "id", "") or "")
    chat = getattr(deal, "chat", None)
    chat_id = str(getattr(chat, "id", "") or "")

    if (not chat_id or not item_id) and deal_id:
        try:
            full_deal = await ctx.get_deal(deal_id)
            if full_deal:
                if not item_id and getattr(full_deal, "item", None):
                    item = full_deal.item
                    item_id = str(getattr(item, "id", "") or "")
                if not chat_id and getattr(full_deal, "chat", None):
                    chat = full_deal.chat
                    chat_id = str(getattr(chat, "id", "") or "")
        except Exception:
            pass

    if not item_id or not deal_id:
        return

    async with ctx.db() as session:
        # Exact lot ID first, then the longest explicitly configured keyword.
        rule = await resolve_lot_rule(
            session, account.id, item_id, str(getattr(item, "name", "") or ""),
            str(getattr(item, "slug", "") or ""),
        )
        if not rule:
            return

        # 2. Проверяем, не отправлялся ли уже заказ для этой сделки
        existing = await session.scalar(
            select(AutoSmmOrder).where(
                AutoSmmOrder.account_id == account.id,
                AutoSmmOrder.deal_id == deal_id,
            )
        )
        if existing:
            return

        # 3. Получаем конфигурацию API панели
        cfg = await session.scalar(
            select(AutoSmmConfig).where(AutoSmmConfig.account_id == account.id)
        )
        if not cfg or not cfg.api_key_encrypted:
            await ctx.notify(
                f"⚠️ <b>AutoSMM: Не настроен API-ключ!</b>\n\n"
                f"Покупатель оплатил лот <b>{html.escape(rule.lot_title)}</b>.\n"
                f"Сделка: <code>{deal_id}</code>\n"
                f"<i>Укажите API-ключ в панели AutoSMM в боте!</i>"
            )
            return

        cipher = getattr(ctx, "cipher", None)
        if not cipher:
            from app.handlers import svc
            cipher = svc().cipher
        try:
            api_key = cipher.decrypt(cfg.api_key_encrypted) if cipher else None
        except Exception:
            api_key = None
        if not api_key:
            await ctx.notify("⚠️ AutoSMM: не удалось прочитать ключ панели. Сохраните ключ заново.")
            return

        api_url = cfg.api_url or "https://smmway.com/api/v2"
        service_id = rule.service_id
        service_name = rule.service_name or f"Услуга #{service_id}"
        quantity = rule.quantity
        rule_auto_sent = rule.auto_sent

    # 4. Извлекаем ссылку покупателя
    link = _extract_target_link(deal)
    if not link:
        logger.info("AutoSMM: Ссылка для накрутки не найдена в сделке %s", deal_id)
        await ctx.notify(
            f"⚠️ <b>AutoSMM</b>: По сделке <code>{deal_id}</code> ({html.escape(rule.lot_title)}) "
            f"не удалось автоматически распознать ссылку покупателя в заказе.\n"
            f"Требуется ручное оформление услуги (#{service_id}, кол-во: <b>{quantity}</b>)."
        )
        return

    if link.startswith("@"):
        link = f"https://t.me/{link[1:]}"

    # Reserve the unique deal before the network mutation. A timeout or a
    # concurrent callback must not cause duplicate paid orders.
    try:
        client = SmmApiClient(api_url=api_url, api_key=api_key)
    except Exception:
        await ctx.notify("⚠️ AutoSMM: проверьте HTTPS-адрес SMM-панели.")
        return
    async with ctx.db() as session:
        order_rec = AutoSmmOrder(
            account_id=account.id, deal_id=deal_id, chat_id=chat_id,
            smm_order_id="0", service_id=service_id, service_name=service_name,
            link=link, quantity=quantity, status="Requesting", deal_completed=False,
            provider_url=client.api_url,
        )
        session.add(order_rec)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return

    status, smm_order_id, error = "Unknown", "0", None
    try:
        resp = await client.create_order(service_id=service_id, link=link, quantity=quantity)
        if resp.get("error"):
            status, error = "Rejected", str(resp["error"])[:250]
        elif raw_id := provider_id(resp.get("order")):
            smm_order_id, status = raw_id, "Pending"
        else:
            error = "Панель не вернула ID заказа. Проверьте его вручную, не отправляйте повторно."
    except Exception as exc:
        logger.warning("AutoSMM order result unknown for deal %s (%s)", deal_id, type(exc).__name__)
        error = "Ответ панели не получен. Проверьте заказ вручную, не отправляйте повторно."

    async with ctx.db() as session:
        record = await session.scalar(select(AutoSmmOrder).where(
            AutoSmmOrder.account_id == account.id, AutoSmmOrder.deal_id == deal_id,
        ))
        if record:
            record.smm_order_id, record.status = smm_order_id, status
            await session.commit()

    if status != "Pending":
        await ctx.notify(
            f"⚠️ AutoSMM: заказ по сделке <code>{html.escape(deal_id)}</code> не подтверждён. "
            f"{html.escape(error or 'Проверьте панель вручную.')}"
        )
        return

    # 7. Автозавершение сделки в статус SENT (выполнен)
    cfg_auto = getattr(ctx, "config", {}) or {}
    auto_complete = cfg_auto.get("auto_confirm_deal", True) and rule_auto_sent
    sent_ok = False
    if auto_complete:
        try:
            await ctx.update_deal(deal_id, ItemDealStatuses.SENT)
            sent_ok = True
        except Exception as exc:
            logger.warning("Could not auto-complete AutoSMM deal %s via ctx.update_deal: %s", deal_id, exc)
            try:
                if hasattr(ctx.client, "update_deal"):
                    await ctx.client.update_deal(deal_id, ItemDealStatuses.SENT)
                    sent_ok = True
                elif hasattr(ctx.client, "call"):
                    await ctx.client.call("update_deal", deal_id=deal_id, new_status=ItemDealStatuses.SENT)
                    sent_ok = True
            except Exception as e2:
                logger.warning("Fallback auto-complete deal failed: %s", e2)

        if sent_ok:
            async with ctx.db() as session:
                rec = await session.scalar(
                    select(AutoSmmOrder).where(
                        AutoSmmOrder.account_id == account.id,
                        AutoSmmOrder.deal_id == deal_id,
                    )
                )
                if rec:
                    rec.deal_completed = True
                    await session.commit()

    # 8. Сообщение покупателю в чат сделки
    if chat_id:
        try:
            await ctx.send_chat(
                chat_id,
                f"Здравствуйте! Спасибо за заказ.\n\n"
                f"🚀 Ваш заказ успешно отправлен в работу в системе накрутки!\n"
                f"🎯 Услуга: {service_name}\n"
                f"🔢 Количество: {quantity} шт.\n"
                f"🔗 Ссылка: {link}\n\n"
                f"Статус можно проверить у продавца. Подтверждайте получение только после выполнения заказа."
            )
        except Exception:
            pass

    # 9. Уведомление продавца в Telegram
    notify_tg = cfg_auto.get("notify_tg", True)
    if notify_tg:
        await ctx.notify(
            f"🚀 <b>AutoSMM: Заказ успешно отправлен в работу!</b>\n\n"
            f"📦 Товар: <b>{html.escape(rule.lot_title)}</b>\n"
            f"🆔 Сделка: <code>{deal_id}</code>\n"
            f"⚡ SMM Заказ: <code>#{smm_order_id}</code>\n"
            f"🎯 Услуга: #{service_id} ({html.escape(service_name)})\n"
            f"🔗 Ссылка: <code>{html.escape(link)}</code>\n"
            f"🔢 Количество: <b>{quantity} шт.</b>\n"
            f"Статус Playerok: {'«Выполнен»' if sent_ok else 'без изменений'}"
        )
