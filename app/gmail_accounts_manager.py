from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import logging
import re
import time
from typing import Any

import pyotp
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, PlayerokAccount

logger = logging.getLogger("gmail_accounts")


class GmailStock(Base):
    """Склад аккаунтов Gmail с TOTP-ключами."""
    __tablename__ = "gmail_stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(190), nullable=False, index=True)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    totp_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    recovery_email: Mapped[str | None] = mapped_column(String(190), nullable=True)
    
    # Статус: 'available' (готов к продаже), 'issued' (выдан покупателю)
    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False, index=True)
    issued_deal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GmailLotRule(Base):
    """Привязка лотов Playerok к автовыдаче Gmail."""
    __tablename__ = "gmail_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_gmail_lot_rule"),
    )


class GmailDeal(Base):
    """История сделок и привязка выданных аккаунтов к чату сделки."""
    __tablename__ = "gmail_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_stocks.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(190), nullable=False)
    code_requests_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_code_sent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deal_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_gmail_deal"),
    )


def clean_totp_secret(secret: str) -> str:
    """Очищает секретный ключ TOTP от пробелов, дефисов и приводит к верхнему регистру."""
    return re.sub(r"[\s\-_]+", "", secret.strip()).upper()


def generate_totp_code(secret: str) -> tuple[str | None, int]:
    """
    Генерирует 6-значный TOTP-код и оставшееся время жизни в секундах.
    Возвращает (code, remaining_seconds).
    Если ключ невалидный — возвращает (None, 0).
    """
    cleaned = clean_totp_secret(secret)
    if not cleaned:
        return None, 0
    try:
        totp = pyotp.TOTP(cleaned)
        code = totp.now()
        time_remaining = 30 - int(time.time()) % 30
        return code, time_remaining
    except Exception as exc:
        logger.warning("Ошибка генерации TOTP-кода: %s", exc)
        return None, 0


def format_delivery_message(email_addr: str, password: str, totp_secret: str) -> str:
    """
    Формирует сообщение автовыдачи для покупателя в чат Playerok.
    Нейтральный текст без триггерных стоп-слов.
    """
    return (
        "Здравствуйте! Спасибо за покупку.\n\n"
        "Ваши данные для авторизации:\n"
        f"Почта: {email_addr}\n"
        f"Пароль: {password}\n"
        f"2FA Секрет: {clean_totp_secret(totp_secret)}\n\n"
        "Для получения 6-значного кода подтверждения напишите в этот чат команду:\n"
        "#2fa\n\n"
        "(Также код можно сгенерировать самостоятельно на 2fa.live, вставив 2FA Секрет)."
    )


def format_totp_reply(code: str, remaining_sec: int) -> str:
    """Формирует ответ на запрос команды #2fa в чате Playerok."""
    return (
        f"Ваш код подтверждения: {code}\n"
        f"(Действителен ещё {remaining_sec} сек. Если не успели, напишите #2fa ещё раз)."
    )
