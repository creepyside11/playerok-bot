from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.roblox_vc_manager import (
    RobloxDeal,
    RobloxLotRule,
    RobloxStock,
    format_roblox_delivery_message,
    generate_roblox_totp,
)
from playerokapi.enums import ItemDealStatuses

logger = logging.getLogger("plugin.roblox_vc_seller")

PLUGIN_META = {
    "id": "roblox_vc_seller",
    "name": "Roblox Voice Chat Автовыдача",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Специализированная автовыдача Roblox аккаунтов с включенным голосовым чатом (Voice Chat / 13+). "
        "Выдает логин, пароль, статус VC, баланс робуксов и опционально 2FA (TOTP) ключ. "
        "Если привязан 2FA — покупатель по команде #2fa или #код мгновенно получает свежий код входа."
    ),
    "settings": {
        "auto_complete": {
            "type": "bool",
            "name": "Автозавершение сделки (SENT)",
            "default": True,
            "description": "Автоматически отмечать сделку выполненной после выдачи аккаунта покупателю",
        },
        "notify_seller": {
            "type": "bool",
            "name": "Уведомлять продавца в Telegram",
            "default": True,
            "description": "Присылать отчет об успешной выдаче аккаунта и остатке на складе в Telegram",
        },
    },
}


async def on_load(ctx: Any) -> None:
    logger.info("Roblox Voice Chat Seller plugin loaded.")


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
        # Ищем активное правило привязки лота к Roblox VC
        rules = (await session.scalars(
            select(RobloxLotRule).where(
                RobloxLotRule.account_id == account.id,
                RobloxLotRule.enabled.is_(True),
            )
        )).all()

        rule = None
        for r in rules:
            if str(r.lot_id).strip() == str(item_id).strip() or str(r.lot_id).strip() == "*":
                rule = r
                break

        # Если не нашли по прямому ID, проверяем slug или название лота
        if not rule and getattr(deal, "item", None):
            item_slug = str(getattr(deal.item, "slug", "") or "").strip()
            item_title = str(getattr(deal.item, "name", "") or "").strip().lower()
            for r in rules:
                if (item_slug and r.lot_id == item_slug) or (item_title and r.lot_title.strip().lower() in item_title):
                    rule = r
                    break

        if not rule:
            return

        # Проверяем, не выдавался ли уже аккаунт для этой сделки
        existing = await session.scalar(
            select(RobloxDeal).where(
                RobloxDeal.account_id == account.id,
                RobloxDeal.deal_id == deal_id,
            )
        )
        if existing:
            return

        # Ищем свободный аккаунт на складе
        stock = await session.scalar(
            select(RobloxStock).where(
                RobloxStock.account_id == account.id,
                RobloxStock.status == "available",
            ).order_by(RobloxStock.id.asc())
        )

        if not stock:
            # Склад пуст — уведомляем продавца
            logger.warning(
                "Roblox VC stock empty on account %s (deal %s)",
                account.id, deal_id,
            )
            msg = (
                f"⚠️ <b>Внимание: склад Roblox Voice Chat пуст!</b>\n\n"
                f"Товар: <b>{html.escape(rule.lot_title)}</b>\n"
                f"Сделка: <code>{deal_id}</code>\n\n"
                f"Покупатель оплатил заказ, но свободных аккаунтов на складе нет. "
                f"Пожалуйста, выдайте данные вручную в чате с покупателем."
            )
            await ctx.notify(msg)
            return

        # Расшифровываем пароль и 2FA ключ (если есть)
        cipher = getattr(ctx, "cipher", None)
        if cipher:
            try:
                password = cipher.decrypt(stock.password_encrypted)
            except Exception:
                password = stock.password_encrypted
            if stock.totp_secret_encrypted:
                try:
                    totp_secret = cipher.decrypt(stock.totp_secret_encrypted)
                except Exception:
                    totp_secret = stock.totp_secret_encrypted
            else:
                totp_secret = None
        else:
            password = stock.password_encrypted
            totp_secret = stock.totp_secret_encrypted

        lot_name = getattr(item, "name", None) or rule.lot_title

        # Формируем итоговое сообщение покупателю
        delivery_text = format_roblox_delivery_message(
            template=rule.delivery_template,
            lot_name=lot_name,
            username=stock.username,
            password=password,
            totp_secret=totp_secret,
            robux_balance=stock.robux_balance,
            comment=stock.comment,
        )

        # Отправляем в чат сделки на Playerok
        try:
            await ctx.send_chat(chat_id, delivery_text)
            logger.info("Sent Roblox VC account for deal %s in chat %s", deal_id, chat_id)
        except Exception as exc:
            logger.error("Failed to send Roblox VC account in chat %s: %s", chat_id, exc)
            await ctx.notify(
                f"❌ <b>Ошибка отправки аккаунта Roblox:</b>\n"
                f"Сделка: <code>{deal_id}</code>\n"
                f"Ошибка: {html.escape(str(exc))}"
            )
            return

        # Помечаем аккаунт как выданный
        now = datetime.now(timezone.utc)
        stock.status = "issued"
        stock.issued_deal_id = deal_id
        stock.issued_chat_id = chat_id
        stock.issued_at = now

        # Записываем сделку
        rbx_deal = RobloxDeal(
            account_id=account.id,
            deal_id=deal_id,
            chat_id=chat_id,
            stock_id=stock.id,
            username=stock.username,
            deal_completed=False,
        )
        session.add(rbx_deal)
        await session.commit()

        # Автозавершение сделки (SENT), если включено
        cfg = getattr(ctx, "config", {}) or {}
        auto_complete = cfg.get("auto_complete", True) and rule.auto_sent
        if auto_complete:
            sent_ok = False
            try:
                await ctx.update_deal(deal_id, ItemDealStatuses.SENT)
                sent_ok = True
            except Exception as exc:
                logger.warning("Could not auto-complete deal %s via ctx.update_deal: %s, trying client fallback", deal_id, exc)
                try:
                    if hasattr(ctx.client, "update_deal"):
                        await ctx.client.update_deal(deal_id, ItemDealStatuses.SENT)
                        sent_ok = True
                    elif hasattr(ctx.client, "call"):
                        await ctx.client.call("update_deal", deal_id=deal_id, new_status=ItemDealStatuses.SENT)
                        sent_ok = True
                    elif hasattr(ctx.client, "set_deal_status"):
                        await ctx.client.set_deal_status(deal_id, ItemDealStatuses.SENT)
                        sent_ok = True
                except Exception as e2:
                    logger.warning("Retry auto-complete deal %s failed: %s", deal_id, e2)

            if sent_ok:
                rbx_deal.deal_completed = True
                await session.commit()
                logger.info("Auto-completed Roblox deal %s with status SENT", deal_id)

        # Уведомляем продавца в Telegram
        notify_seller = cfg.get("notify_seller", True)
        if notify_seller:
            remaining = (await session.scalar(
                select(func.count(RobloxStock.id)).where(
                    RobloxStock.account_id == account.id,
                    RobloxStock.status == "available",
                )
            )) or 0

            robux_str = f" | 💰 {stock.robux_balance:,} R$" if stock.robux_balance else ""
            totp_str = " | 🔐 2FA: Да" if stock.totp_secret_encrypted else " | 2FA: Нет"

            await ctx.notify(
                f"🎙️ <b>Успешная автовыдача Roblox Voice Chat!</b>\n\n"
                f"📦 Лот: <b>{html.escape(lot_name)}</b>\n"
                f"🆔 Сделка: <code>{deal_id}</code>\n\n"
                f"👤 Логин: <code>{html.escape(stock.username)}</code>{robux_str}{totp_str}\n"
                f"📊 Остаток на складе: <b>{remaining} шт.</b>\n"
                f"Статус сделки: <b>{'Завершена (SENT)' if rbx_deal.deal_completed else 'Ожидает'}</b>"
            )


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    """Обрабатывает запросы покупателя в чате с командой получения 2FA (#2fa, #код, #code)."""
    text = str(getattr(message, "text", "") or "").strip().lower()
    if not text:
        return

    # Проверяем запросы 2FA
    is_2fa_request = any(k in text for k in ("#2fa", "#код", "#code", "2fa", "код", "code", "дай код"))
    is_help_request = text in {"#roblox", "#help", "#помощь", "#vc"}

    if not (is_2fa_request or is_help_request):
        return

    chat_id = str(getattr(chat, "id", "") or "")
    if not chat_id:
        return

    async with ctx.db() as session:
        # Ищем последнюю сделку в этом чате
        deal_record = await session.scalar(
            select(RobloxDeal).where(
                RobloxDeal.account_id == ctx.account.id,
                RobloxDeal.chat_id == chat_id,
            ).order_by(RobloxDeal.id.desc())
        )
        if not deal_record:
            return

        stock = await session.get(RobloxStock, deal_record.stock_id)
        if not stock:
            return

        cipher = getattr(ctx, "cipher", None)
        totp_secret = None
        if stock.totp_secret_encrypted and cipher:
            try:
                totp_secret = cipher.decrypt(stock.totp_secret_encrypted)
            except Exception:
                totp_secret = stock.totp_secret_encrypted
        elif stock.totp_secret_encrypted:
            totp_secret = stock.totp_secret_encrypted

        # Если у аккаунта есть 2FA и запрошен код
        if totp_secret and is_2fa_request:
            code, remaining_sec = generate_roblox_totp(totp_secret)
            if code:
                reply = (
                    f"🔢 <b>Ваш код двухфакторной аутентификации Roblox:</b>\n\n"
                    f"👉 <code>{code}</code> 👈\n\n"
                    f"⏳ Действует ещё: <b>{remaining_sec} сек.</b>\n"
                    f"<i>Введите код в окне подтверждения Roblox.</i>"
                )
                try:
                    await ctx.send_chat(chat_id, reply)
                    logger.info("Sent Roblox 2FA code %s to buyer in chat %s", code, chat_id)
                except Exception as exc:
                    logger.debug("Failed to send 2FA code to chat: %s", exc)
                return

        # Если 2FA не включен на аккаунте
        if is_2fa_request and not totp_secret:
            reply = (
                f"ℹ️ На данном аккаунте <code>{html.escape(stock.username)}</code> двухфакторная аутентификация (2FA) отключена.\n"
                f"Вы можете сразу войти в Roblox по логину и паролю без дополнительных кодов!"
            )
            try:
                await ctx.send_chat(chat_id, reply)
            except Exception as exc:
                logger.debug("Failed to send 2fa info reply: %s", exc)
            return

        # Памятка помощи #help / #roblox
        reply = (
            f"ℹ️ <b>Памятка по входу в Roblox Voice Chat:</b>\n\n"
            f"👤 Логин: <code>{html.escape(stock.username)}</code>\n"
            f"🔊 Voice Chat уже верифицирован и активен в настройках приватности.\n"
            f"💡 Чтобы включить микрофон в игре: Settings ⚙️ -> Privacy -> Use microphone to chat with voice.\n"
            f"{'💡 Для получения кода входа отправьте команду <code>#2fa</code>.' if totp_secret else ''}"
        )
        try:
            await ctx.send_chat(chat_id, reply)
        except Exception as exc:
            logger.debug("Failed to send roblox help message: %s", exc)
