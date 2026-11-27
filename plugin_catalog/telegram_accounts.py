from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import logging
import re
import time
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.handlers import svc
from app.telegram_accounts_manager import (
    default_telegram_api_hash,
    default_telegram_api_id,
    TelegramAccountDeal,
    TelegramAccountStock,
    TelegramAccountsConfig,
    available_stock_query,
    bound_lot_rule,
    fetch_latest_code_from_telethon,
    format_code_message,
    format_code_repeat_message,
    format_code_waiting_message,
    format_code_after_revoke_message,
    format_delivery_message,
    format_exit_already_revoked_message,
    format_exit_cancelled_message,
    format_exit_check_failed_message,
    format_exit_confirm_message,
    format_exit_done_message,
    format_exit_expired_message,
    format_exit_no_buyer_session_message,
    format_exit_not_issued_message,
    format_seller_called_message,
    buyer_session_present,
    list_authorizations,
    logout_telethon_session,
)
from app.telegram_commands import (
    CODE, EXIT_ALIASES, NO_ALIASES, SELLER_ALIASES, YES_ALIASES,
    command_argument, is_command,
)
from app.worker import claim_event
from playerokapi.enums import ItemDealDirections, ItemDealStatuses

logger = logging.getLogger("plugin.telegram_accounts")
PAID_STATUSES = {ItemDealStatuses.PAID, ItemDealStatuses.PENDING}
INTRO_RETRY_DELAY = timedelta(minutes=1)
#: Один вызов продавца на чат в минуту — покупатель может нажать несколько раз.
SELLER_CALL_COOLDOWN = 60.0
_seller_calls: dict[str, float] = {}
#: Повторный #код в пределах этого окна отвечаем мгновенно из уже известного кода,
#: чтобы не дёргать Telethon и не ловить флуд-лимиты.
CODE_MIN_INTERVAL = 2.5
_code_calls: dict[str, float] = {}
#: Сколько ждём #да/#нет после #выход: chat_id -> момент истечения (monotonic).
EXIT_CONFIRM_TTL = 600.0
_pending_exit: dict[str, float] = {}

PLUGIN_META = {
    "id": "telegram_accounts",
    "name": "Telegram Accounts",
    "version": "1.3.0",
    "author": "Playerok Bot",
    "description": (
        "Автовыдача Telegram-профилей по странам с получением кода через Telethon. "
        "После оплаты выдает номер и ожидает команду #код (можно повторять), находит код в Telethon, "
        "передает покупателю и автоматически переводит сделку в статус «Выполнен». "
        "Команда #продавец зовёт продавца: в Telegram приходит крупное сообщение, закреплённое в чате. "
        "Команда #выход (с подтверждением #да/#нет) завершает сессию продавца через auth.logOut — "
        "профиль остаётся только у покупателя."
    ),
    "settings": {
        "default_api_id": {"label": "Telegram API ID", "type": "str", "default": str(default_telegram_api_id())},
        "default_api_hash": {"label": "Telegram API Hash", "type": "str", "default": ""},
    },
}


async def on_deal(ctx: Any, deal: Any) -> bool:
    """Return True for paid sales managed by this plugin."""
    if getattr(deal, "direction", None) != ItemDealDirections.OUT or getattr(deal, "status", None) not in PAID_STATUSES:
        return False
    deal_id = str(getattr(deal, "id", "") or "")
    if not deal_id:
        return False
    item = getattr(deal, "item", None)
    item_id = str(getattr(item, "id", "") or "")
    chat_id = str(getattr(getattr(deal, "chat", None), "id", "") or "")
    if not chat_id or not item_id or not getattr(item, "name", None):
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
            item = getattr(full_deal, "item", None) or item
            item_id = str(getattr(item, "id", "") or "")
            chat_id = chat_id or str(getattr(getattr(full_deal, "chat", None), "id", "") or "")

    account = ctx.account
    seller_id = getattr(getattr(item, "user", None), "id", None)
    if seller_id and str(seller_id) != account.playerok_user_id:
        logger.warning("Telegram sale %s seller does not match the configured account", deal_id)
        return False
    async with ctx.db() as session:
        rule = await bound_lot_rule(session, account.id, item_id, getattr(item, "name", None) or "")
        if rule is None or not rule.enabled:
            return False
        if not chat_id:
            logger.warning("Telegram sale %s has no buyer chat yet", deal_id)
            return True

        assigned = select(TelegramAccountDeal).where(
            TelegramAccountDeal.account_id == account.id, TelegramAccountDeal.deal_id == deal_id,
        )
        record = await session.scalar(assigned)
        if record is None:
            available = await available_stock_query(session, account.id, rule.country)
            stock = await session.scalar(available.order_by(TelegramAccountStock.id).limit(1))
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
        # Снимаем состояние ДО захвата аренды: у новой сделки intro_sent = None, и раньше
        # это ошибочно считалось «повторной попыткой», из-за чего перед самой первой
        # отправкой делался лишний запрос истории чата — отсюда была задержка в минуту.
        row = (await session.execute(
            select(TelegramAccountDeal.intro_attempted_at, TelegramAccountDeal.created_at)
            .where(TelegramAccountDeal.id == record.id)
        )).first()
        prior_attempt_at, created_at = row if row else (None, None)
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
    stage = "история чата"
    try:
        # Сверяемся с историей, только если отправка могла пройти раньше: либо попытка
        # уже фиксировалась, либо сделка висит дольше окна повтора (старые записи).
        uncertain = prior_attempt_at is not None or _stale(created_at, now - INTRO_RETRY_DELAY)
        already_sent = uncertain and await initial_message_in_history(ctx, record, text)
        if not already_sent:
            stage = "отправка"
            message = await ctx.client.send_message(record.chat_id, text, mark_chat_as_read=False)
            if not getattr(message, "id", None):
                raise RuntimeError("Playerok did not confirm the initial message")
    except Exception as exc:
        code = str(getattr(exc, "error_code", None) or "")
        detail = code if re.fullmatch(r"[A-Z0-9_]{1,60}", code) else type(exc).__name__
        async with ctx.db() as session:
            await session.execute(update(TelegramAccountDeal).where(
                TelegramAccountDeal.id == record.id, TelegramAccountDeal.intro_sent.is_not(True),
            ).values(intro_error=f"{stage} · {detail}"))
            await session.commit()
        logger.warning("Telegram sale %s initial message unconfirmed (%s)", record.deal_id, detail)
        return

    async with ctx.db() as session:
        await session.execute(update(TelegramAccountDeal).where(
            TelegramAccountDeal.id == record.id,
        ).values(intro_sent=True, intro_error=None))
        await session.commit()
    if not already_sent:
        await notify_seller(
            ctx, record.deal_id,
            f"📱 <b>Покупателю отправлена инструкция</b>\n\n"
            f"Сделка <code>{html.escape(record.deal_id)}</code>\n"
            f"Страна <b>{html.escape(record.country)}</b>"
        )


def _stale(created_at: Any, threshold: datetime) -> bool:
    """True, если запись создана раньше порога (значение из SQLite бывает без tzinfo)."""
    if not isinstance(created_at, datetime):
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at < threshold


async def notify_seller(ctx: Any, deal_id: str, text: str) -> None:
    try:
        await ctx.notify(text)
    except Exception as exc:
        logger.warning("Telegram sale %s seller notification failed (%s)", deal_id, type(exc).__name__)


async def initial_message_in_history(ctx: Any, record: TelegramAccountDeal, text: str) -> bool:
    """Reconcile an uncertain send before trying it again.

    Одна страница последних сообщений: этого достаточно, чтобы понять, ушла ли
    инструкция, и это на порядок быстрее пяти последовательных запросов.
    """
    created = record.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    older_than = created - timedelta(minutes=1)
    page = await ctx.client.call("get_chat_messages", record.chat_id, count=24, after_cursor=None)
    if page is None:
        raise RuntimeError("Could not verify prior delivery in Playerok chat")
    for message in page.messages or []:
        if message is None:
            continue
        if timestamp := getattr(message, "created_at", None):
            try:
                if datetime.fromisoformat(timestamp) < older_than:
                    return False
            except (TypeError, ValueError):
                pass
        if getattr(message, "text", None) == text and str(getattr(
            getattr(message, "user", None), "id", "",
        )) == str(ctx.account.playerok_user_id) and not getattr(message, "deleted_at", None):
            return True
    return False


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    """Обрабатывает сообщения в чате: #код выдаёт код, #продавец зовёт продавца."""
    raw = getattr(message, "text", "") or ""
    if not raw.strip():
        return

    chat_id = str(getattr(chat, "id", "") or "")
    if not chat_id:
        return

    if is_command(raw, SELLER_ALIASES):
        await call_seller(ctx, chat_id, message, command_argument(raw, SELLER_ALIASES))
        return
    if is_command(raw, EXIT_ALIASES):
        await request_exit(ctx, chat_id)
        return
    # #да / #нет — ответ на подтверждение выхода; вне подтверждения игнорируем.
    if is_command(raw, YES_ALIASES):
        await confirm_exit(ctx, chat_id, approved=True)
        return
    if is_command(raw, NO_ALIASES):
        await confirm_exit(ctx, chat_id, approved=False)
        return
    if not is_command(raw, (CODE,)):
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
        if deal_record.session_revoked:
            await ctx.send_chat(chat_id, format_code_after_revoke_message())
            return

        stock = await session.get(TelegramAccountStock, deal_record.stock_id)
        if not stock or not stock.session_encrypted:
            await ctx.send_chat(chat_id, "⚠️ Данные для авторизации не найдены. Свяжитесь с продавцом.")
            return

        cfg = await session.scalar(
            select(TelegramAccountsConfig).where(TelegramAccountsConfig.account_id == account.id)
        )
        api_id = cfg.api_id if cfg and cfg.api_hash else int(ctx.config.get("default_api_id") or default_telegram_api_id())
        api_hash = (cfg.api_hash if cfg else "") or str(ctx.config.get("default_api_hash") or default_telegram_api_hash())

    if not api_hash:
        await ctx.send_chat(chat_id, "Настройки Telegram API не заполнены. Свяжитесь с продавцом.")
        return

    # Дешифруем строку сессии Telethon
    cipher = svc().cipher
    session_str = cipher.decrypt(stock.session_encrypted)
    if not session_str:
        await ctx.send_chat(chat_id, "⚠️ Ошибка безопасности сессии. Свяжитесь с продавцом.")
        return

    # Повторный запрос в пределах пары секунд отдаём сразу, без обращения к Telegram.
    now = time.monotonic()
    if now - _code_calls.get(chat_id, 0) < CODE_MIN_INTERVAL:
        cached = (format_code_repeat_message(deal_record.code_sent) if deal_record.code_sent
                  else format_code_waiting_message(deal_record.phone))
        await ctx.send_chat(chat_id, cached)
        return
    if len(_code_calls) > 2000:
        _code_calls.clear()
    _code_calls[chat_id] = now

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


async def call_seller(ctx: Any, chat_id: str, message: Any, question: str) -> None:
    """#продавец: крупное закреплённое сообщение в Telegram плюс ответ покупателю."""
    now = time.monotonic()
    if now - _seller_calls.get(chat_id, 0) < SELLER_CALL_COOLDOWN:
        return
    if len(_seller_calls) > 500:
        _seller_calls.clear()
    _seller_calls[chat_id] = now

    buyer = getattr(getattr(message, "user", None), "username", None) or "Покупатель"
    try:
        await ctx.send_chat(chat_id, format_seller_called_message())
    except Exception as exc:
        logger.warning("Не удалось ответить покупателю в чате %s (%s)", chat_id, type(exc).__name__)
    await ctx.call_seller(chat_id, buyer=buyer, question=question)


async def _latest_deal(ctx: Any, chat_id: str) -> Any:
    async with ctx.db() as session:
        return await session.scalar(
            select(TelegramAccountDeal)
            .where(
                TelegramAccountDeal.account_id == ctx.account.id,
                TelegramAccountDeal.chat_id == chat_id,
            )
            .order_by(TelegramAccountDeal.id.desc())
        )


async def _session_material(ctx: Any, record: Any) -> tuple[str, int, str] | None:
    """Расшифрованная строка сессии и настройки API для сделки."""
    async with ctx.db() as session:
        stock = await session.get(TelegramAccountStock, record.stock_id)
        if not stock or not stock.session_encrypted:
            return None
        cfg = await session.scalar(
            select(TelegramAccountsConfig).where(TelegramAccountsConfig.account_id == ctx.account.id)
        )
        api_id = cfg.api_id if cfg and cfg.api_hash else int(ctx.config.get("default_api_id") or default_telegram_api_id())
        api_hash = (cfg.api_hash if cfg else "") or str(ctx.config.get("default_api_hash") or default_telegram_api_hash())
        encrypted = stock.session_encrypted
    if not api_hash:
        return None
    session_str = svc().cipher.decrypt(encrypted)
    if not session_str:
        return None
    return session_str, api_id, api_hash


async def request_exit(ctx: Any, chat_id: str) -> None:
    """#выход: просим подтверждение, потому что коды перестанут приходить."""
    record = await _latest_deal(ctx, chat_id)
    if record is None:
        return
    if record.session_revoked:
        await ctx.send_chat(chat_id, format_exit_already_revoked_message())
        return
    if not record.code_sent:
        await ctx.send_chat(chat_id, format_exit_not_issued_message())
        return
    if len(_pending_exit) > 2000:
        _pending_exit.clear()
    _pending_exit[chat_id] = time.monotonic() + EXIT_CONFIRM_TTL
    await ctx.send_chat(chat_id, format_exit_confirm_message())


async def confirm_exit(ctx: Any, chat_id: str, *, approved: bool) -> None:
    """#да / #нет после #выход."""
    deadline = _pending_exit.get(chat_id)
    if deadline is None:
        return
    _pending_exit.pop(chat_id, None)
    if time.monotonic() > deadline:
        await ctx.send_chat(chat_id, format_exit_expired_message())
        return
    if not approved:
        await ctx.send_chat(chat_id, format_exit_cancelled_message())
        return
    await perform_exit(ctx, chat_id)


async def perform_exit(ctx: Any, chat_id: str) -> None:
    """Завершает нашу сессию, но только если покупатель уже вошёл."""
    record = await _latest_deal(ctx, chat_id)
    if record is None:
        return
    if record.session_revoked:
        await ctx.send_chat(chat_id, format_exit_already_revoked_message())
        return

    material = await _session_material(ctx, record)
    if material is None:
        await ctx.send_chat(chat_id, "⚠️ Данные для авторизации не найдены. Свяжитесь с продавцом.")
        return
    session_str, api_id, api_hash = material

    # Проверка перед выходом: без устройства покупателя выходить нельзя,
    # иначе код будет некуда прислать и заказ станет невыполнимым.
    entries = await list_authorizations(session_str, api_id, api_hash)
    if entries is None:
        await ctx.send_chat(chat_id, format_exit_check_failed_message())
        return
    if not buyer_session_present(entries):
        await ctx.send_chat(chat_id, format_exit_no_buyer_session_message())
        return

    if not await logout_telethon_session(session_str, api_id, api_hash):
        await ctx.send_chat(
            chat_id,
            "⚠️ Не удалось завершить сессию. Попробуйте ещё раз или напишите #продавец.",
        )
        await notify_seller(
            ctx, record.deal_id,
            f"⚠️ <b>Не удалось завершить сессию</b>\n\n"
            f"Сделка <code>{html.escape(record.deal_id)}</code>\n"
            "Покупатель просил выход по команде #выход.",
        )
        return

    async with ctx.db() as session:
        saved = await session.get(TelegramAccountDeal, record.id)
        if saved is not None:
            saved.session_revoked = True
            await session.commit()

    await ctx.send_chat(chat_id, format_exit_done_message())
    await notify_seller(
        ctx, record.deal_id,
        f"🔒 <b>Покупатель завершил сессию</b>\n\n"
        f"Сделка <code>{html.escape(record.deal_id)}</code>\n"
        f"Страна <b>{html.escape(record.country)}</b>\n\n"
        "Сессия удалена из списка устройств. Строка сессии оставлена в БД как недействительная.",
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
