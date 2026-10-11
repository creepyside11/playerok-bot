from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import pyotp
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

logger = logging.getLogger("roblox_vc_manager")

DEFAULT_ROBLOX_DELIVERY_TEMPLATE = (
    "🎙️ <b>Данные вашего Roblox аккаунта с Voice Chat:</b>\n"
    "📦 Товар: <b>{lot_name}</b>\n\n"
    "👤 <b>Логин (Username):</b> <code>{username}</code>\n"
    "🔑 <b>Пароль:</b> <code>{password}</code>\n"
    "{totp_block}"
    "{robux_block}"
    "{age_verified_block}"
    "\n{comment_block}"
    "🔊 <b>Статус Voice Chat:</b> 🟢 Включен и проверен (13+ возраст подтвержден)\n\n"
    "📱 <b>Инструкция по входу в Roblox:</b>\n"
    "1. Перейдите на roblox.com или откройте приложение Roblox.\n"
    "2. Введите указанный логин и пароль.{totp_instruction}\n"
    "3. Перейдите в Settings ⚙️ -> Privacy -> включите микрофон/Voice Chat.\n\n"
    "🤝 <i>Спасибо за покупку! Пожалуйста, проверьте аккаунт и подтвердите получение заказа.</i>"
)


class RobloxStock(Base):
    """Склад аккаунтов Roblox с включенным Voice Chat."""
    __tablename__ = "roblox_stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Необязательный секретный ключ 2FA (TOTP)
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Дополнительные свойства аккаунта Roblox
    robux_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_voice_chat_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    has_verified_badge: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False, index=True)  # available, issued
    issued_deal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RobloxLotRule(Base):
    """Привязка лотов Playerok к автовыдаче Roblox Voice Chat."""
    __tablename__ = "roblox_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    delivery_template: Mapped[str] = mapped_column(Text, default=DEFAULT_ROBLOX_DELIVERY_TEMPLATE, nullable=False)
    auto_sent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_roblox_lot_rule"),
    )


class RobloxDeal(Base):
    """История выданных аккаунтов Roblox по сделкам."""
    __tablename__ = "roblox_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("roblox_stocks.id", ondelete="CASCADE"), nullable=False
    )
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    deal_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_roblox_deal"),
    )


def clean_roblox_totp_secret(secret: str | None) -> str:
    """Очищает секретный ключ 2FA от пробелов и дефисов."""
    if not secret:
        return ""
    return re.sub(r"[\s\-_]+", "", secret.strip()).upper()


def generate_roblox_totp(secret: str | None) -> tuple[str | None, int]:
    """Генерирует свежий 6-значный TOTP код Roblox Authenticator и секунды до смены."""
    cleaned = clean_roblox_totp_secret(secret)
    if not cleaned:
        return None, 0
    try:
        totp = pyotp.TOTP(cleaned)
        code = totp.now()
        rem = 30 - int(time.time()) % 30
        return code, rem
    except Exception as exc:
        logger.debug("Roblox TOTP generate error: %s", exc)
        return None, 0


def format_roblox_delivery_message(
    template: str,
    lot_name: str,
    username: str,
    password: str,
    totp_secret: str | None = None,
    robux_balance: int | None = None,
    comment: str | None = None,
) -> str:
    """Форматирует сообщение автовыдачи аккаунта Roblox."""
    tmpl = template or DEFAULT_ROBLOX_DELIVERY_TEMPLATE
    clean_secret = clean_roblox_totp_secret(totp_secret)

    if clean_secret:
        totp_block = (
            f"🔐 <b>2FA Ключ:</b> <code>{clean_secret}</code>\n"
            f"💡 <i>Отправьте <code>#2fa</code> или <code>#код</code> в этот чат для получения кода входа!</i>\n"
        )
        totp_instruction = "\n   Если запросит код Authenticator — напишите в этот чат <code>#2fa</code>."
    else:
        totp_block = "🔐 <b>2FA (Authenticator):</b> Отключен (вход без доп. кодов)\n"
        totp_instruction = ""

    robux_block = f"💰 <b>Баланс Robux:</b> <code>{robux_balance:,} R$</code>\n" if robux_balance is not None else ""
    age_verified_block = "✅ <b>Верификация:</b> Возраст подтвержден (13+ ID Verified)\n"
    comment_block = f"📝 <b>Инструкция продавца:</b>\n{html.escape(comment)}\n" if comment else ""

    text = tmpl
    text = text.replace("{lot_name}", html.escape(lot_name))
    text = text.replace("{username}", html.escape(username))
    text = text.replace("{password}", html.escape(password))
    text = text.replace("{totp_block}", totp_block)
    text = text.replace("{totp_instruction}", totp_instruction)
    text = text.replace("{robux_block}", robux_block)
    text = text.replace("{age_verified_block}", age_verified_block)
    text = text.replace("{comment_block}", comment_block)

    return text.strip()


def parse_roblox_bulk_lines(text: str) -> list[dict[str, Any]]:
    """
    Парсит строки формата:
    username:password
    username:password:totp_secret
    username:password:totp_secret:comment_or_robux
    """
    results = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts: list[str]
        if ":" in line:
            parts = [p.strip() for p in line.split(":")]
        elif ";" in line:
            parts = [p.strip() for p in line.split(";")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            parts = [p.strip() for p in line.split()]

        if len(parts) >= 2:
            uname = parts[0]
            pwd = parts[1]
            totp = parts[2] if len(parts) > 2 and parts[2] else None
            extra = parts[3] if len(parts) > 3 else None

            robux = None
            comment = extra
            if extra and extra.isdigit():
                robux = int(extra)

            results.append({
                "username": uname,
                "password": pwd,
                "totp_secret": totp,
                "robux_balance": robux,
                "comment": comment,
            })
    return results
