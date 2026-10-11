from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

from sqlalchemy import select

from app.autosmm_manager import (
    AutoSmmConfig,
    AutoSmmLotRule,
    AutoSmmOrder,
    SmmApiClient,
)
from playerokapi.enums import ItemDealStatuses

logger = logging.getLogger("plugin.autosmm")

PLUGIN_META = {
    "id": "autosmm",
    "name": "AutoSMM (Накрутка / SMM панель)",
    "version": "2.0.0",
    "author": "Playerok Bot",
    "description": (
        "Автоматическое создание заказов в SMM-панелях (smmway и любых сервисах со стандартным SMM API v2) "
        "при покупке лотов на Playerok. Удобный графический интерфейс привязки товаров, каталог услуг, "
        "баланс и автоматический перевод заказов в статус «Выполнен»."
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
        # 1. Проверяем правило привязки лота
        rules = (await session.scalars(
            select(AutoSmmLotRule).where(
                AutoSmmLotRule.account_id == account.id,
                AutoSmmLotRule.enabled.is_(True),
            )
        )).all()

        rule = None
        for r in rules:
            if str(r.lot_id).strip() == str(item_id).strip() or str(r.lot_id).strip() == "*":
                rule = r
                break

        if not rule and getattr(deal, "item", None):
            item_slug = str(getattr(deal.item, "slug", "") or "").strip()
            item_title = str(getattr(deal.item, "name", "") or "").strip().lower()
            for r in rules:
                if (item_slug and r.lot_id == item_slug) or (item_title and r.lot_title.strip().lower() in item_title):
                    rule = r
                    break

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
            api_key = cipher.decrypt(cfg.api_key_encrypted) if cipher else cfg.api_key_encrypted
        except Exception:
            api_key = cfg.api_key_encrypted

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

    # 5. Отправляем заказ в SMM панель
    client = SmmApiClient(api_url=api_url, api_key=api_key)
    try:
        resp = await client.create_order(service_id=service_id, link=link, quantity=quantity)
    except Exception as exc:
        logger.error("AutoSMM create_order failed for deal %s: %s", deal_id, exc)
        await ctx.notify(
            f"❌ <b>AutoSMM: Ошибка соединения с SMM-панелью:</b>\n"
            f"Сделка: <code>{deal_id}</code>\n"
            f"Ошибка: <code>{html.escape(str(exc)[:250])}</code>"
        )
        return

    if "error" in resp:
        err_msg = str(resp.get("error"))
        logger.error("AutoSMM returned error for deal %s: %s", deal_id, err_msg)
        await ctx.notify(
            f"❌ <b>AutoSMM: Сервис вернул отказ</b> по сделке <code>{deal_id}</code>:\n"
            f"Причина: <b>{html.escape(err_msg)}</b>\n"
            f"Услуга: #{service_id}, Ссылка: <code>{html.escape(link)}</code>"
        )
        return

    smm_order_id = str(resp.get("order") or "0")

    # 6. Сохраняем в БД
    async with ctx.db() as session:
        order_rec = AutoSmmOrder(
            account_id=account.id,
            deal_id=deal_id,
            chat_id=chat_id,
            smm_order_id=smm_order_id,
            service_id=service_id,
            service_name=service_name,
            link=link,
            quantity=quantity,
            status="Pending",
            deal_completed=False,
        )
        session.add(order_rec)
        await session.commit()

    # 7. Автозавершение сделки в статус SENT (выполнен)
    cfg_auto = getattr(ctx, "config", {}) or {}
    auto_complete = cfg_auto.get("auto_confirm_deal", True) and rule_auto_sent
    if auto_complete:
        sent_ok = False
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
                f"Выполнение начнется в течение нескольких минут. Пожалуйста, подтвердите получение заказа!"
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
            f"✅ Статус заказа на Playerok: «Выполнен»"
        )
