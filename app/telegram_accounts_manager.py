from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import html
import logging
import os
import re
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, PlayerokAccount

logger = logging.getLogger("telegram_accounts")

def default_telegram_api_id() -> int:
    try:
        return max(1, int(os.getenv("TELEGRAM_API_ID", "2040")))
    except ValueError:
        return 2040


def default_telegram_api_hash() -> str:
    value = os.getenv("TELEGRAM_API_HASH", "").strip()
    return value if re.fullmatch(r"[0-9a-fA-F]{32}", value) else ""

COUNTRY_PRESETS = [
    ("🇷🇺 Россия", "+7"),
    ("🇰🇿 Казахстан", "+7"),
    ("🇧🇾 Беларусь", "+375"),
    ("🇺🇿 Узбекистан", "+998"),
    ("🇺🇦 Украина", "+380"),
    ("🇺🇸 США", "+1"),
    ("🇮🇩 Индонезия", "+62"),
    ("🇬🇧 Великобритания", "+44"),
    ("🇩🇪 Германия", "+49"),
]


class TelegramAccountLotRule(Base):
    __tablename__ = "telegram_account_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    keyword: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    country: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_tgacc_lot_rule"),
    )


class TelegramAccountStock(Base):
    __tablename__ = "telegram_account_stock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    country: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(64), nullable=False)
    session_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False, index=True)
    issued_deal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TelegramAccountDeal(Base):
    __tablename__ = "telegram_account_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account_stock.id", ondelete="CASCADE"), nullable=False
    )
    phone: Mapped[str] = mapped_column(String(64), nullable=False)
    country: Mapped[str] = mapped_column(String(128), nullable=False)
    intro_sent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    intro_attempted_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    intro_error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    code_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    code_sent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deal_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    session_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_tgacc_deal"),
    )


class TelegramAccountsConfig(Base):
    __tablename__ = "telegram_account_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    api_id: Mapped[int] = mapped_column(Integer, default=default_telegram_api_id, nullable=False)
    api_hash: Mapped[str] = mapped_column(String(128), default=default_telegram_api_hash, nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def normalized_keyword(text: str) -> str:
    return " ".join(text.casefold().split())


def valid_keyword(text: Any) -> bool:
    return isinstance(text, str) and 1 <= len(text.strip()) <= 128 and any(ch.isalnum() for ch in text)


async def bind_keyword(db: Any, account: PlayerokAccount, keyword: str, country: str) -> TelegramAccountLotRule:
    label = " ".join(keyword.split())
    key = "keyword:" + sha256(normalized_keyword(label).encode()).hexdigest()
    async with db() as session:
        await session.get(PlayerokAccount, account.id, with_for_update=True)
        row = await session.scalar(select(TelegramAccountLotRule).where(
            TelegramAccountLotRule.account_id == account.id, TelegramAccountLotRule.lot_id == key,
        ))
        if row is None:
            row = TelegramAccountLotRule(account_id=account.id, lot_id=key, enabled=True)
            session.add(row)
        row.keyword = row.lot_title = label
        row.country = country
        await session.commit()
        return row


async def bound_lot_rule(
    session: Any, account_id: Any, lot_id: str, lot_title: str = "",
) -> TelegramAccountLotRule | None:
    rules = select(TelegramAccountLotRule).where(TelegramAccountLotRule.account_id == account_id)
    if lot_id:
        direct = await session.scalar(rules.where(
            TelegramAccountLotRule.lot_id == lot_id, TelegramAccountLotRule.keyword.is_(None),
        ))
        if direct is not None:
            return direct
    if not lot_title:
        return None
    title = normalized_keyword(lot_title)
    keywords = await session.scalars(rules.where(
        TelegramAccountLotRule.keyword.is_not(None), TelegramAccountLotRule.enabled.is_(True),
    ))
    matches = [rule for rule in keywords if normalized_keyword(rule.keyword) in title]
    return min(matches, key=lambda rule: (-len(normalized_keyword(rule.keyword)), rule.id), default=None)


def country_key(country: str) -> str:
    key = " ".join(country.casefold().split())
    for preset, _ in COUNTRY_PRESETS:
        tag, name = preset.casefold().split(" ", 1)
        if key in {f"{tag} {name}", name}:
            return name
    return key


async def available_stock_query(session: Any, account_id: Any, country: str) -> Any:
    # A custom country can be entered without the flag used by the stock menu.
    available = (
        TelegramAccountStock.account_id == account_id,
        TelegramAccountStock.status == "available",
    )
    countries = await session.scalars(select(TelegramAccountStock.country).where(*available).distinct())
    key = country_key(country)
    labels = [label for label in countries if country_key(label) == key]
    return select(TelegramAccountStock).where(*available, TelegramAccountStock.country.in_(labels))


def extract_5digit_code(text: str) -> str | None:
    """Извлекает 5-значный код подтверждения из текста сообщения Telegram."""
    if not text:
        return None

    # Поиск 5-значного кода с ключевыми словами
    patterns = [
        r"(?:code|код|вход|login|confirmation)[^\d]{0,20}(\d{5})\b",
        r"\b(\d{5})\b",
        r"(?:code|код|вход|login)[^\d]{0,20}(\d{3})[- ](\d{2})\b",
        r"\b(\d{3})[- ](\d{2})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            if len(m.groups()) == 1:
                return m.group(1)
            elif len(m.groups()) == 2:
                return f"{m.group(1)}{m.group(2)}"
    return None


def format_delivery_message(country: str, phone: str) -> str:
    """Сообщение покупателю при выдаче. Ни в коем случае не упоминает слово 'аккаунт'."""
    return (
        f"Здравствуйте! Спасибо за покупку 🙌\n\n"
        f"Ваш профиль ({country}):\n"
        f"📱 Номер: {phone}\n\n"
        "Как войти:\n"
        "1. Откройте Telegram и войдите по этому номеру.\n"
        "2. Запросите код при входе — он придёт внутрь Telegram.\n"
        "3. Отправьте сюда команду #код — я перешлю вам этот код.\n\n"
        "Команду #код можно отправлять повторно сколько угодно раз: если код не пришёл, "
        "подождите немного и запросите его снова.\n\n"
        "🤖 Есть вопрос по заказу — отправьте #аск и свой вопрос, ответит помощник (ИИ).\n"
        "🙋 Нужен человек — отправьте #продавец, и продавец подключится к этому чату.\n\n"
        "❤️ Если что-то пошло не так — сначала напишите продавцу командой #продавец. "
        "Давайте решать любые вопросы мирно и без обращения в поддержку: "
        "продавец всегда пойдёт вам навстречу и решит вашу проблему."
    )


def format_code_message(code: str) -> str:
    """Сообщение с кодом. Ни в коем случае не упоминает слово 'аккаунт'."""
    return (
        f"🔑 Проверочный код: {code}\n\n"
        "Заказ полностью выполнен! Проверьте вход и подтвердите получение заказа.\n\n"
        "Нужен новый код? Запросите его в Telegram и отправьте #код ещё раз — "
        "код можно получать повторно.\n\n"
        "🔒 Когда закончите, отправьте #выход — я отключу свою сессию, "
        "и доступ к профилю останется только у вас."
    )


def format_code_waiting_message(phone: str) -> str:
    """Сообщение при отсутствии кода. Без слова 'аккаунт'."""
    return (
        f"⏳ Код пока не поступил на номер {phone}.\n\n"
        "Запросите отправку кода в приложении и отправьте #код ещё раз через несколько секунд. "
        "Команду #код можно отправлять повторно сколько угодно раз."
    )


def format_code_repeat_message(code: str) -> str:
    """Повторное сообщение с кодом. Без слова 'аккаунт'."""
    return (
        f"🔑 Ранее полученный код: {code}\n\n"
        "Код можно запрашивать повторно: запросите новый код в приложении и отправьте #код снова."
    )


def format_seller_called_message() -> str:
    """Ответ покупателю после команды #продавец. Без слова 'аккаунт'."""
    return (
        "🙋 Передал ваш вопрос продавцу — он подключится в этом чате как можно скорее.\n\n"
        "Пока ждёте, можно отправить #аск и задать вопрос помощнику (ИИ)."
    )


#: Переподключение к Telegram стоит несколько секунд, поэтому клиенты
#: переиспользуются, а весь запрос кода ограничен по времени.
CODE_FETCH_TIMEOUT = 20.0
MAX_CACHED_CLIENTS = 16
_CLIENTS: dict[str, Any] = {}
_CLIENT_LOCKS: dict[str, asyncio.Lock] = {}


def _session_key(session_str: str, api_id: int) -> str:
    return sha256(f"{api_id}:{session_str}".encode()).hexdigest()


async def _drop_client(key: str) -> None:
    client = _CLIENTS.pop(key, None)
    if client is None:
        return
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception:
        pass


async def reset_telethon_cache() -> None:
    """Закрыть кешированные клиенты (используется в тестах и при выключении)."""
    for key in list(_CLIENTS):
        await _drop_client(key)
    _CLIENT_LOCKS.clear()


async def _client_for(session_str: str, api_id: int, api_hash: str, key: str) -> Any:
    """Один подключённый клиент на сессию вместо нового коннекта на каждый код."""
    cached = _CLIENTS.get(key)
    if cached is not None and cached.is_connected():
        return cached
    await _drop_client(key)

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(session_str), api_id, api_hash,
        connection_retries=2, retry_delay=1, timeout=15,
        auto_reconnect=True, flood_sleep_threshold=60,
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=CODE_FETCH_TIMEOUT)
        authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=CODE_FETCH_TIMEOUT)
    except BaseException:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise
    if not authorized:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise RuntimeError("unauthorized")

    while len(_CLIENTS) >= MAX_CACHED_CLIENTS:
        await _drop_client(next(iter(_CLIENTS)))
    _CLIENTS[key] = client
    return client


async def _newest_code(client: Any) -> tuple[str | None, str | None]:
    """Новый код обычно лежит в последнем сообщении диалога — это один запрос."""
    dialogs = await client.get_dialogs(limit=8)
    candidates = [dialog for dialog in dialogs if getattr(dialog, "message", None)]
    if not candidates:
        return None, "no_messages"
    candidates.sort(
        key=lambda dialog: getattr(dialog.message, "date", None) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for dialog in candidates:
        text = getattr(dialog.message, "message", "") or ""
        code = extract_5digit_code(text)
        if code:
            return code, text

    # Резерв: короткий параллельный просмотр самых свежих диалогов.
    entities = [getattr(dialog, "entity", None) for dialog in candidates[:4]]
    entities = [entity for entity in entities if entity is not None]
    if not entities:
        return None, "no_messages"
    pages = await asyncio.gather(
        *(client.get_messages(entity, limit=3) for entity in entities), return_exceptions=True,
    )
    for page in pages:
        if isinstance(page, BaseException):
            continue
        for message in page or []:
            text = getattr(message, "message", "") or ""
            code = extract_5digit_code(text)
            if code:
                return code, text
    return None, "code_not_found"


def format_exit_confirm_message() -> str:
    """Подтверждение выхода. Без слова 'аккаунт'."""
    return (
        "⚠️ Подтвердите выход.\n\n"
        "После выхода я больше не смогу присылать коды: команда #код перестанет работать, "
        "а восстановить доступ можно будет только через поддержку Telegram.\n\n"
        "Отправьте #да — чтобы выйти, или #нет — чтобы отменить."
    )


def format_exit_done_message() -> str:
    """Сессия продавца завершена. Без слова 'аккаунт'."""
    return (
        "✅ Готово. Моя сессия завершена и удалена из списка устройств — "
        "теперь в профиле только ваше устройство.\n"
        "Спасибо за покупку! 🙌\n\n"
        "Если понадобится помощь — напишите #продавец."
    )


def format_exit_cancelled_message() -> str:
    """Покупатель отменил выход. Без слова 'аккаунт'."""
    return "👍 Отмена. Сессия остаётся активной, команда #код работает как обычно."


def format_exit_not_issued_message() -> str:
    """Выход доступен только после выдачи кода. Без слова 'аккаунт'."""
    return "Сначала получите код по команде #код — выход станет доступен после выдачи."


def format_exit_no_buyer_session_message() -> str:
    """Покупатель ещё не вошёл: выходить нельзя, иначе код некуда прислать."""
    return (
        "⚠️ Не вижу вашего устройства в профиле.\n\n"
        "Похоже, вход по номеру ещё не выполнен, поэтому я не буду выходить: "
        "иначе код некуда будет прислать.\n\n"
        "1. Войдите в Telegram по номеру из этого чата.\n"
        "2. Запросите код при входе.\n"
        "3. Отправьте #код — я перешлю код.\n"
        "4. После входа повторите #выход."
    )


def format_exit_expired_message() -> str:
    """Подтверждение не дождалось ответа. Без слова 'аккаунт'."""
    return "⌛ Подтверждение устарело. Отправьте #выход заново, если всё ещё хотите завершить сессию."


def format_exit_check_failed_message() -> str:
    """Не удалось проверить список устройств — выходить не рискуем."""
    return (
        "⚠️ Не удалось проверить устройства в профиле, поэтому выход отменён — так безопаснее.\n\n"
        "Попробуйте ещё раз через минуту, отправив #выход."
    )


def format_exit_already_revoked_message() -> str:
    """Сессия уже завершена ранее. Без слова 'аккаунт'."""
    return "🔒 Сессия уже завершена. Коды больше не присылаются — доступ к профилю только у вас."


def format_code_after_revoke_message() -> str:
    """Попытка получить код после выхода. Без слова 'аккаунт'."""
    return (
        "🔒 Сессия завершена, коды больше не присылаются.\n\n"
        "Если нужна помощь — отправьте #продавец."
    )


async def fetch_latest_code_from_telethon(
    session_str: str,
    api_id: int,
    api_hash: str,
) -> tuple[str | None, str | None]:
    """Возвращает самый свежий 5-значный код из сессии Telethon.

    Клиент переиспользуется между вызовами, поэтому повторный запрос кода
    занимает доли секунды. Весь запрос ограничен ``CODE_FETCH_TIMEOUT``, чтобы
    медленная сеть не блокировала общий цикл опроса.
    """
    try:
        from telethon.sessions import StringSession  # noqa: F401  (проверка зависимости)
    except ImportError:
        logger.error("Telethon не установлен")
        return None, "Telethon not installed"

    key = _session_key(session_str, api_id)
    lock = _CLIENT_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        try:
            client = await _client_for(session_str, api_id, api_hash, key)
            return await asyncio.wait_for(_newest_code(client), timeout=CODE_FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            await _drop_client(key)
            logger.warning("Получение кода из Telethon не уложилось в %s с", CODE_FETCH_TIMEOUT)
            return None, "timeout"
        except Exception as exc:
            await _drop_client(key)
            logger.warning("Ошибка получения кода из Telethon: %s", type(exc).__name__)
            return None, str(exc)


async def list_authorizations(
    session_str: str, api_id: int, api_hash: str,
) -> list[dict[str, Any]] | None:
    """Список устройств, авторизованных в профиле. ``None`` — проверить не удалось."""
    try:
        from telethon.tl import functions
    except ImportError:
        logger.error("Telethon не установлен")
        return None

    key = _session_key(session_str, api_id)
    lock = _CLIENT_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        try:
            client = await _client_for(session_str, api_id, api_hash, key)
            result = await asyncio.wait_for(
                client(functions.account.GetAuthorizationsRequest()), timeout=CODE_FETCH_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("Список устройств недоступен: %s", type(exc).__name__)
            return None

    entries: list[dict[str, Any]] = []
    for auth in getattr(result, "authorizations", None) or []:
        entries.append({
            "hash": getattr(auth, "hash", None),
            "current": bool(getattr(auth, "current", False)),
            "device": str(getattr(auth, "device_model", "") or ""),
            "platform": str(getattr(auth, "platform", "") or ""),
            "app": str(getattr(auth, "app_name", "") or ""),
        })
    return entries


def buyer_session_present(entries: list[dict[str, Any]] | None) -> bool:
    """Вошёл ли покупатель: есть ли в профиле устройство, кроме нашего."""
    if not entries:
        return False
    current = [entry for entry in entries if entry.get("current")]
    others = [entry for entry in entries if not entry.get("current")]
    if current:
        return bool(others)
    # Флаг current не пришёл ни у одной записи — судим по количеству устройств.
    return len(entries) > 1


async def logout_telethon_session(session_str: str, api_id: int, api_hash: str) -> bool:
    """Завершить нашу сессию: auth.LogOutRequest + удаление из списка устройств."""
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        logger.error("Telethon не установлен")
        return False

    key = _session_key(session_str, api_id)
    lock = _CLIENT_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        client = _CLIENTS.pop(key, None)
        if client is None or not client.is_connected():
            client = TelegramClient(
                StringSession(session_str), api_id, api_hash,
                connection_retries=2, retry_delay=1, timeout=15,
            )
            try:
                await asyncio.wait_for(client.connect(), timeout=CODE_FETCH_TIMEOUT)
            except Exception as exc:
                logger.warning("Не удалось подключиться для выхода: %s", type(exc).__name__)
                return False
        try:
            return bool(await asyncio.wait_for(client.log_out(), timeout=CODE_FETCH_TIMEOUT))
        except Exception as exc:
            logger.warning("Не удалось завершить сессию: %s", type(exc).__name__)
            return False
        finally:
            _CLIENTS.pop(key, None)
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass
