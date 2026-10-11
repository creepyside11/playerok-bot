from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.kosell_manager import (
    KosellAccountConfig,
    KosellApiError,
    KosellClient,
    KosellDeal,
    KosellLotRule,
    format_kosell_code_reply,
    format_kosell_delivery_message,
)
from playerokapi.enums import ItemDealStatuses

logger = logging.getLogger("plugin.kosell_seller")

PLUGIN_META = {
    "id": "kosell_seller",
    "name": "KOSell Steam Аренда",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Полная автоматизация аренды Steam-аккаунтов через сервис KOSell (kosell.store). "
        "При оплате заказа на Playerok автоматически закупает аренду через API KOSell, "
        "выдает покупателю логин, пароль, срок действия и моментально передает Steam Guard "
        "коды прямо в чат Playerok по команде #код или #guard."
    ),
    "settings": {
        "auto_complete": {
            "type": "bool",
            "name": "Автозавершение сделки (SENT)",
            "default": True,
            "description": "Автоматически отмечать сделку выполненной после выдачи арендованного аккаунта покупателю",
        },
        "notify_seller": {
            "type": "bool",
            "name": "Уведомлять продавца в Telegram",
            "default": True,
            "description": "Присылать отчет в Telegram о созданной аренде, списании баланса и сроке",
        },
    },
}


async def on_load(ctx: Any) -> None:
    logger.info("KOSell Steam Rental plugin loaded.")


async def on_deal(ctx: Any, deal: Any) -> None:
    """Вызывается при появлении нового оплаченного заказа."""
    account = ctx.account
    item = getattr(deal, "item", None)
    item_id = str(getattr(item, "id", "") or "")
    deal_id = str(getattr(deal, "id", "") or "")
    chat = getattr(deal, "chat", None)
    chat_id = str(getattr(chat, "id", "") or "")

    # Если chat_id или item_id не загружены в объекте сделки, подтягиваем через API
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
        except Exception as exc:
            logger.debug("on_deal get_deal fallback error: %s", exc)

    if not item_id or not deal_id or not chat_id:
        return

    async with ctx.db() as session:
        # 1. Проверяем наличие активного правила привязки лота к игре KOSell
        rules = (await session.scalars(
            select(KosellLotRule).where(
                KosellLotRule.account_id == account.id,
                KosellLotRule.enabled.is_(True),
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

        # 2. Проверяем, не выдавалась ли уже аренда для этой сделки
        existing = await session.scalar(
            select(KosellDeal).where(
                KosellDeal.account_id == account.id,
                KosellDeal.deal_id == deal_id,
            )
        )
        if existing:
            return

        # 3. Получаем конфигурацию API-ключа KOSell
        cfg = await session.scalar(
            select(KosellAccountConfig).where(KosellAccountConfig.account_id == account.id)
        )
        if not cfg or not cfg.api_key_encrypted:
            await ctx.notify(
                f"⚠️ <b>KOSell: Не настроен API-ключ!</b>\n\n"
                f"Покупатель оплатил лот <b>{html.escape(rule.lot_title)}</b>.\n"
                f"Сделка: <code>{deal_id}</code>\n\n"
                f"<i>Пожалуйста, укажите API-ключ в настройках KOSell в боте!</i>"
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

        currency = cfg.currency or "RUB"
        product_id = rule.product_id
        product_name = rule.product_name
        duration_hours = rule.duration_hours or 1
        template = rule.delivery_template
        rule_auto_sent = rule.auto_sent

    # 4. Обращаемся к API KOSell и закупаем аренду
    kosell_client = KosellClient(api_key=api_key)
    try:
        rental = await kosell_client.create_rental(
            product_id=product_id,
            duration_hours=duration_hours,
            currency=currency,
            idempotency_key=deal_id,
        )
    except KosellApiError as err:
        logger.error("KOSell create_rental failed for deal %s: %s", deal_id, err)
        await ctx.notify(
            f"❌ <b>KOSell: Ошибка создания аренды!</b>\n\n"
            f"Сделка: <code>{deal_id}</code>\n"
            f"Игра: <b>{html.escape(product_name)}</b>\n"
            f"Ошибка API: <code>{html.escape(str(err))}</code>\n\n"
            f"<i>Проверьте баланс на KOSell или наличие свободных аккаунтов.</i>"
        )
        return
    except Exception as exc:
        logger.error("Unexpected error in KOSell rental creation: %s", exc)
        await ctx.notify(
            f"❌ <b>KOSell: Сетевая ошибка при заказе!</b>\n"
            f"Сделка: <code>{deal_id}</code>: {html.escape(str(exc))}"
        )
        return

    rental_uid = rental.get("rental_uid") or ""
    steam_login = rental.get("steam_login") or ""
    price_paid = rental.get("price_paid") or rental.get("price_rub") or 0.0

    # 5. Получаем пароль от выданного аккаунта
    steam_password = ""
    try:
        creds = await kosell_client.get_credentials(rental_uid)
        steam_password = creds.get("steam_password") or ""
    except Exception as exc:
        logger.warning("Could not fetch credentials immediately for %s: %s", rental_uid, exc)

    # Вычисляем время окончания
    expires_str = "1 час"
    expires_at_dt = None
    if rental.get("expires_at"):
        try:
            # ISO timestamp
            exp_raw = rental["expires_at"].replace("Z", "+00:00")
            expires_at_dt = datetime.fromisoformat(exp_raw)
            expires_str = expires_at_dt.strftime("%d.%m.%Y %H:%M UTC")
        except Exception:
            expires_str = str(rental["expires_at"])

    lot_name = getattr(item, "name", None) or rule.lot_title

    # 6. Формируем и отправляем сообщение покупателю в чат Playerok
    delivery_text = format_kosell_delivery_message(
        template=template,
        lot_name=lot_name,
        product_name=product_name,
        duration_hours=duration_hours,
        steam_login=steam_login,
        steam_password=steam_password,
        expires_at=expires_str,
    )

    try:
        await ctx.send_chat(chat_id, delivery_text)
    except Exception as exc:
        logger.error("Failed to send KOSell rental details to chat %s: %s", chat_id, exc)
        await ctx.notify(
            f"❌ <b>Ошибка отправки данных покупателю:</b>\n"
            f"Сделка: <code>{deal_id}</code>\n"
            f"Ошибка: {html.escape(str(exc))}"
        )
        return

    # 7. Записываем сделку в БД
    async with ctx.db() as session:
        deal_record = KosellDeal(
            account_id=account.id,
            deal_id=deal_id,
            chat_id=chat_id,
            rental_uid=rental_uid,
            product_id=product_id,
            product_name=product_name,
            steam_login=steam_login,
            duration_hours=duration_hours,
            price_paid=float(price_paid) if price_paid else None,
            currency=currency,
            expires_at=expires_at_dt,
            deal_completed=False,
        )
        session.add(deal_record)
        await session.commit()

    # 8. Автозавершение сделки (перевод в SENT / выполнен)
    cfg_auto = getattr(ctx, "config", {}) or {}
    auto_complete = cfg_auto.get("auto_complete", True) and rule_auto_sent
    if auto_complete:
        sent_ok = False
        try:
            await ctx.update_deal(deal_id, ItemDealStatuses.SENT)
            sent_ok = True
        except Exception as exc:
            logger.warning("Could not auto-complete KOSell deal %s via ctx.update_deal: %s, fallback to client", deal_id, exc)
            try:
                if hasattr(ctx.client, "update_deal"):
                    await ctx.client.update_deal(deal_id, ItemDealStatuses.SENT)
                    sent_ok = True
                elif hasattr(ctx.client, "call"):
                    await ctx.client.call("update_deal", deal_id=deal_id, new_status=ItemDealStatuses.SENT)
                    sent_ok = True
            except Exception as e2:
                logger.warning("Fallback auto-complete KOSell deal failed: %s", e2)

        if sent_ok:
            async with ctx.db() as session:
                rec = await session.scalar(
                    select(KosellDeal).where(
                        KosellDeal.account_id == account.id,
                        KosellDeal.deal_id == deal_id,
                    )
                )
                if rec:
                    rec.deal_completed = True
                    await session.commit()
            logger.info("Auto-completed KOSell deal %s with status SENT", deal_id)

    # 9. Уведомляем продавца в Telegram
    notify_seller = cfg_auto.get("notify_seller", True)
    if notify_seller:
        # Проверяем остаток баланса KOSell
        bal_str = ""
        try:
            bal = await kosell_client.get_balance()
            bal_str = f"\n💰 Остаток баланса KOSell: <b>{bal.get('balance_rub', 0):,.2f} ₽</b> (или ${bal.get('balance_usd', 0):,.2f})"
        except Exception:
            pass

        await ctx.notify(
            f"🚀 <b>Успешная аренда Steam через KOSell!</b>\n\n"
            f"📦 Лот: <b>{html.escape(lot_name)}</b>\n"
            f"🆔 Сделка: <code>{deal_id}</code>\n"
            f"🎮 Игра: <b>{html.escape(product_name)}</b>\n"
            f"⏱ Время: <b>{duration_hours} ч.</b> (до {expires_str})\n"
            f"👤 Steam логин: <code>{html.escape(steam_login)}</code>\n"
            f"💵 Списано: <b>{price_paid} {currency}</b>{bal_str}\n\n"
            f"✅ Покупатель получил данные. Код Steam Guard выдается по команде <code>#код</code>!"
        )


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    """Обрабатывает запросы покупателя в чате с командой получения кода Steam Guard (#код, #guard, #code)."""
    text = str(getattr(message, "text", "") or "").strip().lower()
    if not text:
        return

    # Проверяем триггеры запроса кода Guard
    is_guard_request = (
        text in {"#код", "#code", "#guard", "#steam", "код", "code", "guard", "дай код", "код стим"}
        or text.startswith("#код")
        or text.startswith("#guard")
        or text.startswith("#code")
    )
    is_help_request = text in {"#help", "#помощь", "#инструкция"}

    if not (is_guard_request or is_help_request):
        return

    chat_id = str(getattr(chat, "id", "") or "")
    if not chat_id:
        return

    account = ctx.account
    async with ctx.db() as session:
        # Ищем последнюю сделку KOSell в этом чате
        deal_record = await session.scalar(
            select(KosellDeal).where(
                KosellDeal.account_id == account.id,
                KosellDeal.chat_id == chat_id,
            ).order_by(KosellDeal.id.desc())
        )
        if not deal_record:
            return

        cfg = await session.scalar(
            select(KosellAccountConfig).where(KosellAccountConfig.account_id == account.id)
        )
        if not cfg or not cfg.api_key_encrypted:
            await ctx.send_chat(chat_id, "⚠️ Сервис временно недоступен. Пожалуйста, напишите продавцу.")
            return

        cipher = getattr(ctx, "cipher", None)
        if not cipher:
            from app.handlers import svc
            cipher = svc().cipher
        try:
            api_key = cipher.decrypt(cfg.api_key_encrypted) if cipher else cfg.api_key_encrypted
        except Exception:
            api_key = cfg.api_key_encrypted

    kosell_client = KosellClient(api_key=api_key)

    if is_guard_request:
        try:
            res = await kosell_client.get_steam_code(deal_record.rental_uid)
            code = res.get("code")
            expires_in = res.get("expires_in")
            success = res.get("success", False)

            if success and code:
                reply = format_kosell_code_reply(code, expires_in)
                await ctx.send_chat(chat_id, reply)

                # Фиксируем в БД
                async with ctx.db() as session:
                    rec = await session.get(KosellDeal, deal_record.id)
                    if rec:
                        rec.code_requests_count += 1
                        rec.last_code_sent = code
                        await session.commit()

                # Уведомляем продавца
                await ctx.notify(
                    f"🔐 <b>Покупатель запросил Steam Guard код!</b>\n\n"
                    f"Сделка: <code>{deal_record.deal_id}</code>\n"
                    f"Игра: <b>{html.escape(deal_record.product_name)}</b>\n"
                    f"Логин: <code>{html.escape(deal_record.steam_login)}</code>\n"
                    f"Код: <code>{code}</code>"
                )
                return
            else:
                err_msg = res.get("error") or "код пока не запрошен"
                await ctx.send_chat(
                    chat_id,
                    f"⏳ <b>Код Steam Guard еще не сгенерирован.</b>\n\n"
                    f"Убедитесь, что вы нажали «Войти» в Steam и приложение запросило код.\n"
                    f"Подождите 5-10 секунд и отправьте <code>#код</code> ещё раз."
                )
                return
        except Exception as exc:
            logger.warning("Error fetching KOSell Steam code for deal %s: %s", deal_record.deal_id, exc)
            await ctx.send_chat(chat_id, "⚠️ Не удалось получить код подтверждения в данный момент. Попробуйте через 10 секунд.")
            return

    if is_help_request:
        await ctx.send_chat(
            chat_id,
            f"ℹ️ <b>Памятка по аренде {html.escape(deal_record.product_name)}:</b>\n\n"
            f"👤 Логин Steam: <code>{html.escape(deal_record.steam_login)}</code>\n"
            f"🔐 Чтобы получить код двухфакторной аутентификации, отправьте <code>#код</code>.\n"
            f"⚠️ Запрещено менять пароли и привязки аккаунта."
        )
