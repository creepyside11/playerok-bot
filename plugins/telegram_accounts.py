from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import logging
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

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
from app.worker import claim_event
from playerokapi.enums import ItemDealDirections, ItemDealStatuses

logger = logging.getLogger("plugin.telegram_accounts")
PAID_STATUSES = {ItemDealStatuses.PAID, ItemDealStatuses.PENDING}
INTRO_RETRY_DELAY = timedelta(minutes=1)

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


async def on_deal(ctx: Any, deal: Any) -> bool:
    """Return True for paid sales managed by this plugin."""
    if getattr(deal, "direction", None) != ItemDealDirections.OUT or getattr(deal, "status", None) not in PAID_STATUSES:
        return False
    deal_id = str(getattr(deal, "id", "") or "")
    if not deal_id:
        return False
    item_id = str(getattr(getattr(deal, "item", None), "id", "") or "")
    chat_id = str(getattr(getattr(deal, "chat", None), "id", "") or "")
    if not chat_id or not item_id:
        try:
            full_deal = await ctx.get_deal(deal_id)
        except Exception as exc:
            logger.warning("Telegram sale %s details unavailable (%s)", deal_id, type(exc).__name__)
            full_deal = None
        if full_deal:
            current_status = getattr(full_deal, "status", None)
            if (current_status is not None and current_status not in PAID_STATUSES
                    or getattr(full_deal, "direction", None) == ItemDealDirections.IN):
                return False
            item_id = item_id or str(getattr(getattr(full_deal, "item", None), "id", "") or "")
            chat_id = chat_id or str(getattr(getattr(full_deal, "chat", None), "id", "") or "")

    account = ctx.account
    async with ctx.db() as session:
        rule = await session.scalar(select(TelegramAccountLotRule).where(
            TelegramAccountLotRule.account_id == account.id,
            TelegramAccountLotRule.lot_id == item_id,
            TelegramAccountLotRule.enabled.is_(True),
        ))
        if not rule:
            return False
        if not chat_id:
            logger.warning("Telegram sale %s has no buyer chat yet", deal_id)
            return True

        assigned = select(TelegramAccountDeal).where(
            TelegramAccountDeal.account_id == account.id, TelegramAccountDeal.deal_id == deal_id,
        )
        record = await session.scalar(assigned)
        if record is None:
            stock = await session.scalar(select(TelegramAccountStock).where(
                TelegramAccountStock.account_id == account.id,
                TelegramAccountStock.country == rule.country,
                TelegramAccountStock.status == "available",
            ).order_by(TelegramAccountStock.id).limit(1))
            if stock:
                reserved = await session.execute(update(TelegramAccountStock).where(
                    TelegramAccountStock.id == stock.id, TelegramAccountStock.status == "available",
                ).values(
                    status="issued", issued_deal_id=deal_id,
                    issued_chat_id=chat_id, issued_at=datetime.now(timezone.utc),
                ))
                if not reserved.rowcount:
                    return True
                record = TelegramAccountDeal(
                    account_id=account.id, deal_id=deal_id, chat_id=chat_id, stock_id=stock.id,
                    phone=stock.phone, country=stock.country, intro_sent=False,
                )
                session.add(record)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    if await session.scalar(assigned) is not None:
                        return True
                    raise

    if record is None:
        if await claim_event(ctx.db, account.id, "tg_stock_empty", deal_id):
            await notify_seller(
                ctx, deal_id,
                "⚠️ <b>Telegram: склад пуст</b>\n\n"
                f"Лот <b>{html.escape(rule.lot_title)}</b>, страна <b>{html.escape(rule.country)}</b>\n"
                f"Сделка <code>{html.escape(deal_id)}</code>\n\n"
                "Плагин повторит выдачу после пополнения склада."
            )
        return True
    if record.chat_id != chat_id:
        logger.warning("Telegram sale %s buyer chat differs from the reserved chat", deal_id)
        return True
    if record.intro_sent or record.code_requested or record.deal_completed:
        return True

    await send_initial_message(ctx, record)
    return True


async def send_initial_message(ctx: Any, record: TelegramAccountDeal) -> None:
    now = datetime.now(timezone.utc)
    async with ctx.db() as session:
        lease = await session.execute(update(TelegramAccountDeal).where(
            TelegramAccountDeal.id == record.id,
            TelegramAccountDeal.intro_sent.is_not(True),
            TelegramAccountDeal.code_requested.is_(False),
            TelegramAccountDeal.deal_completed.is_(False),
            or_(
                TelegramAccountDeal.intro_attempted_at.is_(None),
                TelegramAccountDeal.intro_attempted_at <= now - INTRO_RETRY_DELAY,
            ),
        ).values(intro_attempted_at=now))
        await session.commit()
    if not lease.rowcount:
        return

    text = format_delivery_message(record.country, record.phone)
    try:
        previous_attempt = record.intro_sent is None or record.intro_attempted_at is not None
        already_sent = previous_attempt and await initial_message_in_history(ctx, record, text)
        if not already_sent:
            message = await ctx.client.send_message(record.chat_id, text, mark_chat_as_read=False)
            if not getattr(message, "id", None):
                raise RuntimeError("Playerok did not confirm the initial message")
    except Exception as exc:
        logger.warning("Telegram sale %s initial message unconfirmed (%s)", record.deal_id, type(exc).__name__)
        return

    async with ctx.db() as session:
        await session.execute(update(TelegramAccountDeal).where(TelegramAccountDeal.id == record.id).values(intro_sent=True))
        await session.commit()
    if not already_sent:
        await notify_seller(
            ctx, record.deal_id,
            f"📱 <b>Покупателю отправлена инструкция</b>\n\n"
            f"Сделка <code>{html.escape(record.deal_id)}</code>\n"
            f"Страна <b>{html.escape(record.country)}</b>"
        )


async def notify_seller(ctx: Any, deal_id: str, text: str) -> None:
    try:
        await ctx.notify(text)
    except Exception as exc:
        logger.warning("Telegram sale %s seller notification failed (%s)", deal_id, type(exc).__name__)


async def initial_message_in_history(ctx: Any, record: TelegramAccountDeal, text: str) -> bool:
    """Reconcile an uncertain send before trying it again."""
    created = record.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    older_than = created - timedelta(minutes=1)
    cursor = None
    for _ in range(5):
        page = await ctx.client.call("get_chat_messages", record.chat_id, count=24, after_cursor=cursor)
        if page is None:
            break
        for message in page.messages or []:
            if message is None:
                continue
            if getattr(message, "text", None) == text and str(getattr(
                getattr(message, "user", None), "id", "",
            )) == str(ctx.account.playerok_user_id) and not getattr(message, "deleted_at", None):
                return True
            if timestamp := getattr(message, "created_at", None):
                try:
                    if datetime.fromisoformat(timestamp) < older_than:
                        return False
                except (TypeError, ValueError):
                    pass
        if not getattr(page.page_info, "has_next_page", False):
            return False
        next_cursor = getattr(page.page_info, "end_cursor", None)
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    raise RuntimeError("Could not verify prior delivery in Playerok chat")


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
