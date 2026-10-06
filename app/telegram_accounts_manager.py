from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import logging
import re
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, PlayerokAccount

logger = logging.getLogger("telegram_accounts")

DEFAULT_TG_API_ID = 2040
DEFAULT_TG_API_HASH = "b1844dd2b70111dd37c342977a944a31"

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
    code_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    code_sent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deal_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    api_id: Mapped[int] = mapped_column(Integer, default=DEFAULT_TG_API_ID, nullable=False)
    api_hash: Mapped[str] = mapped_column(String(128), default=DEFAULT_TG_API_HASH, nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


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
        f"Здравствуйте!\n\n"
        f"Ваш профиль ({country}):\n"
        f"📱 Номер: {phone}\n\n"
        "Для получения кода авторизации отправьте в этот чат команду:\n"
        "#код"
    )


def format_code_message(code: str) -> str:
    """Сообщение с кодом. Ни в коем случае не упоминает слово 'аккаунт'."""
    return (
        f"🔑 Проверочный код: {code}\n\n"
        "Заказ полностью выполнен! Пожалуйста, проверьте вход и подтвердите получение заказа."
    )


def format_code_waiting_message(phone: str) -> str:
    """Сообщение при отсутствии кода. Без слова 'аккаунт'."""
    return (
        f"⏳ Код пока не поступил на номер {phone}.\n\n"
        "Пожалуйста, запросите отправку кода в приложении и отправьте команду #код ещё раз через несколько секунд."
    )


def format_code_repeat_message(code: str) -> str:
    """Повторное сообщение с кодом. Без слова 'аккаунт'."""
    return (
        f"🔑 Ранее полученный код: {code}\n\n"
        "Если вы запрашивали новый код, повторите запрос в приложении и отправьте команду #код снова."
    )


async def fetch_latest_code_from_telethon(
    session_str: str,
    api_id: int,
    api_hash: str,
) -> tuple[str | None, str | None]:
    """Подключается к сессии Telethon, находит самый новый чат с сообщением и извлекает 5-значный код."""
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        logger.error("Telethon не установлен")
        return None, "Telethon not installed"

    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None, "unauthorized"

        dialogs = await client.get_dialogs(limit=10)
        if not dialogs:
            return None, "no_dialogs"

        valid_dialogs = [d for d in dialogs if getattr(d, "message", None)]
        if not valid_dialogs:
            return None, "no_messages"

        # Сортируем диалоги по дате последнего сообщения (самый свежий в начале)
        sorted_dialogs = sorted(
            valid_dialogs,
            key=lambda d: getattr(d.message, "date", None) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        for d in sorted_dialogs[:4]:
            msg = d.message
            msg_text = getattr(msg, "message", "") or ""
            code = extract_5digit_code(msg_text)
            if code:
                return code, msg_text

            # Также проверяем последние сообщения внутри этого верхнего диалога
            try:
                entity = getattr(d, "entity", None)
                if entity:
                    recent_msgs = await client.get_messages(entity, limit=3)
                    for rm in recent_msgs:
                        rm_text = getattr(rm, "message", "") or ""
                        code = extract_5digit_code(rm_text)
                        if code:
                            return code, rm_text
            except Exception:
                pass

        return None, "code_not_found"
    except Exception as exc:
        logger.exception("Ошибка получения кода из Telethon: %s", exc)
        return None, str(exc)
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
