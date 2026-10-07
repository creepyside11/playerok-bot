from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import logging
from typing import Any

from sqlalchemy import select

from app.handlers import svc
from app.gmail_accounts_manager import (
    GmailDeal,
    GmailLotRule,
    GmailStock,
    format_delivery_message,
    format_totp_reply,
    generate_totp_code,
)
from playerokapi.enums import ItemDealStatuses

logger = logging.getLogger("plugin.gmail_seller")

PLUGIN_META = {
    "id": "gmail_seller",
    "name": "Gmail 2FA Seller",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Автовыдача аккаунтов Gmail с привязанным 2FA (TOTP). "
        "При оплате выдает Email, Пароль, 2FA-ключ и сразу переводит заказ в выполненные. "
        "По команде #2fa покупатель в любой момент получает свежий 6-значный код прямо в чате."
    ),
    "settings": {
        "auto_complete": {"label": "Автозавершение сделки (SENT)", "type": "bool", "default": True},
    },
}


async def on_deal(ctx: Any, deal: Any) -> None:
    """Вызывается при появлении нового оплаченного заказа."""
    account = ctx.account
    item = getattr(deal, "item", None)
    item_id = str(getattr(item, "id", "") or "")
    if not item_id:
        return

    deal_id = str(getattr(deal, "id", "") or "")
    chat = getattr(deal, "chat", None)
    chat_id = str(getattr(chat, "id", "") or "")
    if not deal_id or not chat_id:
        return

    async with ctx.db() as session:
        # Проверяем, привязан ли лот к автовыдаче Gmail
        rule = await session.scalar(
            select(GmailLotRule).where(
                GmailLotRule.account_id == account.id,
                GmailLotRule.lot_id == item_id,
                GmailLotRule.enabled.is_(True),
            )
        )
        if not rule:
            return

        # Проверяем, не выдавался ли уже аккаунт для этой сделки
        existing = await session.scalar(
            select(GmailDeal).where(
                GmailDeal.account_id == account.id,
                GmailDeal.deal_id == deal_id,
            )
        )
        if existing:
            return

        # Ищем свободный аккаунт в наличии
        stock = await session.scalar(
            select(GmailStock).where(
                GmailStock.account_id == account.id,
                GmailStock.status == "available",
            ).order_by(GmailStock.id)
        )
        if not stock:
            # Склад пуст — уведомляем продавца
            await ctx.notify(
                "⚠️ <b>Gmail: Закончились аккаунты на складе!</b>\n\n"
                f"Покупатель оплатил лот <b>{html.escape(rule.lot_title)}</b>.\n"
                f"Сделка: <code>{html.escape(deal_id)}</code>\n\n"
                "<i>Пожалуйста, срочно пополните склад в меню Gmail Автовыдачи!</i>"
            )
            return

        # Резервируем аккаунт
        stock.status = "issued"
        stock.issued_deal_id = deal_id
        stock.issued_chat_id = chat_id
        stock.issued_at = datetime.now(timezone.utc)

        deal_record = GmailDeal(
            account_id=account.id,
            deal_id=deal_id,
            chat_id=chat_id,
            stock_id=stock.id,
            email=stock.email,
            code_requests_count=0,
            deal_completed=False,
        )
        session.add(deal_record)
        await session.commit()

        stock_email = stock.email
        stock_pwd_enc = stock.password_encrypted
        stock_totp_enc = stock.totp_secret_encrypted

    # Дешифруем данные
    cipher = svc().cipher
    password = cipher.decrypt(stock_pwd_enc)
    totp_secret = cipher.decrypt(stock_totp_enc)

    # Отправляем сообщение покупателю в чат Playerok
    delivery_msg = format_delivery_message(stock_email, password, totp_secret)
    await ctx.send_chat(chat_id, delivery_msg)

    # Автозавершение сделки (перевод в статус SENT / выполнен продавцом)
    auto_complete = bool(ctx.config.get("auto_complete", True))
    if auto_complete:
        try:
            await ctx.client.update_deal(deal_id, ItemDealStatuses.SENT)
            async with ctx.db() as session:
                rec = await session.scalar(
                    select(GmailDeal).where(
                        GmailDeal.account_id == account.id,
                        GmailDeal.deal_id == deal_id,
                    )
                )
                if rec:
                    rec.deal_completed = True
                    await session.commit()
        except Exception as exc:
            logger.warning("Не удалось перевести сделку %s в SENT: %s", deal_id, exc)

    # Уведомляем продавца в Telegram
    await ctx.notify(
        f"📧 <b>Выдан аккаунт Gmail покупателю</b>\n\n"
        f"Сделка: <code>{html.escape(deal_id)}</code>\n"
        f"Товар: <b>{html.escape(rule.lot_title)}</b>\n"
        f"Email: <code>{html.escape(stock_email)}</code>\n"
        f"Пароль: <code>{html.escape(password)}</code>\n"
        f"2FA Секрет: <code>{html.escape(totp_secret)}</code>\n\n"
        "✅ Заказ переведён в статус «Выполнен». Покупатель может получить код по команде <code>#2fa</code>."
    )


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    """Обрабатывает сообщения в чате Playerok. По команде #2fa генерирует и отправляет TOTP-код."""
    raw_text = (getattr(message, "text", "") or "").strip().lower()
    if not raw_text:
        return

    # Проверяем команды: #2fa, 2fa, #код, #code
    is_2fa_command = (
        raw_text == "#2fa"
        or raw_text.startswith("#2fa")
        or raw_text == "2fa"
        or raw_text == "#код"
        or raw_text.startswith("#код")
        or raw_text == "#code"
        or raw_text.startswith("#code")
    )
    if not is_2fa_command:
        return

    chat_id = str(getattr(chat, "id", "") or "")
    if not chat_id:
        return

    account = ctx.account
    async with ctx.db() as session:
        # Находим последнюю сделку по этому чату
        deal_record = await session.scalar(
            select(GmailDeal)
            .where(
                GmailDeal.account_id == account.id,
                GmailDeal.chat_id == chat_id,
            )
            .order_by(GmailDeal.id.desc())
        )
        if not deal_record:
            return

        stock = await session.get(GmailStock, deal_record.stock_id)
        if not stock or not stock.totp_secret_encrypted:
            await ctx.send_chat(chat_id, "⚠️ Данные 2FA не найдены. Обратитесь к продавцу.")
            return

        cipher = svc().cipher
        totp_secret = cipher.decrypt(stock.totp_secret_encrypted)

    code, rem = generate_totp_code(totp_secret)
    if not code:
        await ctx.send_chat(chat_id, "⚠️ Ошибка генерации кода подтверждения. Обратитесь к продавцу.")
        return

    reply_text = format_totp_reply(code, rem)
    await ctx.send_chat(chat_id, reply_text)

    # Обновляем статистику в базе
    async with ctx.db() as session:
        rec = await session.get(GmailDeal, deal_record.id)
        if rec:
            rec.code_requests_count += 1
            rec.last_code_sent = code
            await session.commit()

    # Уведомляем продавца о запросе кода
    await ctx.notify(
        f"🔑 <b>Покупатель запросил 2FA-код!</b>\n\n"
        f"Почта: <code>{html.escape(deal_record.email)}</code>\n"
        f"Сгенерирован код: <code>{code}</code>\n"
        f"Чат сделки: <code>{html.escape(chat_id)}</code>"
    )
