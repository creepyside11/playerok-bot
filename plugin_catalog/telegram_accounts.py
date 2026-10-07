from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import logging
from typing import Any

from sqlalchemy import select

from app.handlers import svc
from app.telegram_accounts_manager import (
    DEFAULT_TG_API_HASH,
    DEFAULT_TG_API_ID,
    TelegramAccountDeal,
    TelegramAccountLotRule,
    TelegramAccountStock,
    TelegramAccountsConfig,
    fetch_latest_code_from_telethon,
    format_code_message,
    format_code_repeat_message,
    format_code_waiting_message,
    format_delivery_message,
)
from playerokapi.enums import ItemDealStatuses

logger = logging.getLogger("plugin.telegram_accounts")

PLUGIN_META = {
    "id": "telegram_accounts",
    "name": "Telegram Accounts",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Автовыдача Telegram-профилей по странам с получением кода через Telethon. "
        "После оплаты выдает номер и ожидает команду #код, по которой находит код в Telethon, "
        "передает покупателю и автоматически переводит сделку в статус «Выполнен»."
    ),
    "settings": {
        "default_api_id": {"label": "Telegram API ID", "type": "str", "default": "2040"},
        "default_api_hash": {"label": "Telegram API Hash", "type": "str", "default": "b1844dd2b70111dd37c342977a944a31"},
    },
}


async def on_deal(ctx: Any, deal: Any) -> None:
    """Вызывается при появлении нового оплаченного заказа."""
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
        except Exception as exc:
            logger.debug("on_deal get_deal fallback error: %s", exc)

    if not item_id or not deal_id or not chat_id:
        return

    async with ctx.db() as session:
        # Проверяем, привязан ли этот лот в правилах плагина
        rule = await session.scalar(
            select(TelegramAccountLotRule).where(
                TelegramAccountLotRule.account_id == account.id,
                TelegramAccountLotRule.lot_id == item_id,
                TelegramAccountLotRule.enabled.is_(True),
            )
        )
        if not rule:
            return

        # Проверяем, не выдавался ли уже профиль для этой сделки
        existing = await session.scalar(
            select(TelegramAccountDeal).where(
                TelegramAccountDeal.account_id == account.id,
                TelegramAccountDeal.deal_id == deal_id,
            )
        )
        if existing:
            return

        # Ищем свободный профиль для заданной страны
        stock = await session.scalar(
            select(TelegramAccountStock).where(
                TelegramAccountStock.account_id == account.id,
                TelegramAccountStock.country == rule.country,
                TelegramAccountStock.status == "available",
            ).order_by(TelegramAccountStock.id)
        )
        if not stock:
            # Склад пуст для этой страны — уведомляем продавца
            await ctx.notify(
                "⚠️ <b>Telegram: Склад пуст!</b>\n\n"
                f"Покупатель оплатил лот <b>{html.escape(rule.lot_title)}</b>.\n"
                f"Требуется страна: <b>{html.escape(rule.country)}</b>\n"
                f"Сделка: <code>{html.escape(deal_id)}</code>\n\n"
                "<i>Пожалуйста, добавьте номер в «Менеджер аккаунтов».</i>"
            )
            return

        # Резервируем профиль
        stock.status = "issued"
        stock.issued_deal_id = deal_id
        stock.issued_chat_id = chat_id
        stock.issued_at = datetime.now(timezone.utc)

        deal_record = TelegramAccountDeal(
            account_id=account.id,
            deal_id=deal_id,
            chat_id=chat_id,
            stock_id=stock.id,
            phone=stock.phone,
            country=stock.country,
            code_requested=False,
            deal_completed=False,
        )
        session.add(deal_record)
        await session.commit()

        stock_phone = stock.phone
        rule_country = rule.country

    # Отправляем сообщение покупателю в чат сделки на Playerok
    # ВНИМАНИЕ: Слово "аккаунт" нигде не используется!
    delivery_text = format_delivery_message(rule_country, stock_phone)
    await ctx.send_chat(chat_id, delivery_text)

    # Уведомляем продавца в Telegram
    await ctx.notify(
        f"📱 <b>Выдан профиль покупателю</b>\n\n"
        f"Сделка: <code>{html.escape(deal_id)}</code>\n"
        f"Страна: <b>{html.escape(rule_country)}</b>\n"
        f"Номер: <code>{html.escape(stock_phone)}</code>\n\n"
        "Покупателю отправлена инструкция для получения кода через <code>#код</code>."
    )


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    """Обрабатывает сообщения в чате. По команде #код запрашивает 5-значный код из Telethon."""
    text = (getattr(message, "text", "") or "").strip().lower()
    if not text:
        return
    if text != "#код" and not text.startswith("#код"):
        return

    chat_id = str(getattr(chat, "id", "") or "")
    if not chat_id:
        return

    account = ctx.account
    async with ctx.db() as session:
        # Находим последнюю активную сделку по этому чату
        deal_record = await session.scalar(
            select(TelegramAccountDeal)
            .where(
                TelegramAccountDeal.account_id == account.id,
                TelegramAccountDeal.chat_id == chat_id,
            )
            .order_by(TelegramAccountDeal.id.desc())
        )
        if not deal_record:
            return

        stock = await session.get(TelegramAccountStock, deal_record.stock_id)
        if not stock or not stock.session_encrypted:
            await ctx.send_chat(chat_id, "⚠️ Данные для авторизации не найдены. Свяжитесь с продавцом.")
            return

        cfg = await session.scalar(
            select(TelegramAccountsConfig).where(TelegramAccountsConfig.account_id == account.id)
        )
        api_id = cfg.api_id if cfg else int(ctx.config.get("default_api_id") or DEFAULT_TG_API_ID)
        api_hash = cfg.api_hash if cfg else str(ctx.config.get("default_api_hash") or DEFAULT_TG_API_HASH)

    # Дешифруем строку сессии Telethon
    cipher = svc().cipher
    session_str = cipher.decrypt(stock.session_encrypted)
    if not session_str:
        await ctx.send_chat(chat_id, "⚠️ Ошибка безопасности сессии. Свяжитесь с продавцом.")
        return

    # Запрашиваем последний 5-значный код из Telethon (можно запрашивать повторно сколько угодно раз)
    code, raw_text = await fetch_latest_code_from_telethon(session_str, api_id, api_hash)
    if not code:
        # Если новый код ещё не поступил, но ранее код уже выдавался
        if deal_record.code_sent:
            repeat_text = format_code_repeat_message(deal_record.code_sent)
            await ctx.send_chat(chat_id, repeat_text)
            return

        # Код ещё не пришёл в Telegram
        waiting_text = format_code_waiting_message(deal_record.phone)
        await ctx.send_chat(chat_id, waiting_text)
        return

    # Код найден! Отправляем покупателю в чат (без слова "аккаунт")
    is_repeated = bool(deal_record.code_sent and deal_record.code_sent == code)
    if is_repeated:
        code_text = format_code_repeat_message(code)
    else:
        code_text = format_code_message(code)
    await ctx.send_chat(chat_id, code_text)

    # Отмечаем сделку выполненной со стороны продавца (SENT)
    try:
        await ctx.client.update_deal(deal_record.deal_id, ItemDealStatuses.SENT)
    except Exception as exc:
        logger.warning("Ошибка перевода сделки %s в SENT: %s", deal_record.deal_id, exc)

    # Обновляем статус сделки в базе (сохраняем последний выданный код)
    async with ctx.db() as session:
        rec = await session.get(TelegramAccountDeal, deal_record.id)
        if rec:
            rec.code_requested = True
            rec.code_sent = code
            rec.deal_completed = True
            await session.commit()

    # Уведомляем продавца
    notify_label = "Повторный код" if is_repeated else "Код подтверждения"
    await ctx.notify(
        f"✅ <b>{notify_label} отправлен покупателю!</b>\n\n"
        f"Сделка: <code>{html.escape(deal_record.deal_id)}</code>\n"
        f"Номер: <code>{html.escape(deal_record.phone)}</code>\n"
        f"Код: <code>{html.escape(code)}</code>\n\n"
        "Заказ переведён в статус «Выполнен», покупателю отправлена просьба подтвердить заказ на Playerok."
    )


async def on_action(ctx: Any, action: str, payload: dict[str, Any]) -> None:
    if action == "stats":
        account = ctx.account
        async with ctx.db() as session:
            count = await session.scalar(
                select(func.count(TelegramAccountStock.id)).where(
                    TelegramAccountStock.account_id == account.id,
                    TelegramAccountStock.status == "available",
                )
            ) or 0
        await ctx.notify(f"📊 Доступно к выдаче номеров: <b>{count}</b>")
