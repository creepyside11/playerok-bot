from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

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

logger = logging.getLogger("supercell_manager")

SUPERCELL_GAMES = {
    "brawl_stars": "Brawl Stars 🥊",
    "clash_royale": "Clash Royale 👑",
    "clash_of_clans": "Clash of Clans 🏰",
    "squad_busters": "Squad Busters 💥",
    "hay_day": "Hay Day 🚜",
}

DEFAULT_SUPERCELL_DELIVERY_TEMPLATE = (
    "⚡ <b>Данные вашего аккаунта {game_name}:</b>\n"
    "📦 Товар: <b>{lot_name}</b>\n\n"
    "📧 <b>Supercell ID (Почта):</b> <code>{email}</code>\n"
    "🔑 <b>Пароль от почты:</b> <code>{password}</code>\n"
    "{stats_block}"
    "{recovery_codes_block}"
    "{mail_access_block}"
    "\n{comment_block}"
    "📱 <b>Инструкция по входу в Supercell ID:</b>\n"
    "1. Откройте {game_name} и зайдите в Настройки ⚙️\n"
    "2. Нажмите «Подключить Supercell ID» -> «Войти»\n"
    "3. Введите указанную почту и получите код (на почте или используйте коды восстановления)\n\n"
    "🤝 <i>Спасибо за покупку! Пожалуйста, проверьте данные и подтвердите заказ на Playerok.</i>"
)


class SupercellStock(Base):
    """Склад аккаунтов Supercell с привязкой к конкретной игре."""
    __tablename__ = "supercell_stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    game_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # brawl_stars, clash_royale, clash_of_clans
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Характеристики аккаунта
    trophies: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Кубки
    town_hall: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Уровень ТХ (для CoC)
    brawlers_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Бравлеры (для BS)
    recovery_codes_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)  # Коды восстановления Supercell ID
    has_mail_access: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # Доступ к почте (родная/авторег)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False, index=True)  # available, issued
    issued_deal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SupercellLotRule(Base):
    """Привязка лота Playerok к автовыдаче Supercell."""
    __tablename__ = "supercell_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    game_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    delivery_template: Mapped[str] = mapped_column(Text, default=DEFAULT_SUPERCELL_DELIVERY_TEMPLATE, nullable=False)
    auto_sent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_supercell_lot_rule"),
    )


class SupercellDeal(Base):
    """История сделок по аккаунтам Supercell."""
    __tablename__ = "supercell_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("supercell_stocks.id", ondelete="CASCADE"), nullable=False
    )
    game_key: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    deal_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_supercell_deal"),
    )


def format_supercell_delivery_message(
    template: str,
    game_key: str,
    lot_name: str,
    email: str,
    password: str,
    trophies: int | None = None,
    town_hall: int | None = None,
    brawlers_count: int | None = None,
    recovery_codes: str | None = None,
    has_mail_access: bool = True,
    comment: str | None = None,
) -> str:
    """Форматирует сообщение покупателю с подстановкой параметров аккаунта Supercell."""
    tmpl = template or DEFAULT_SUPERCELL_DELIVERY_TEMPLATE
    game_name = SUPERCELL_GAMES.get(game_key, game_key.replace("_", " ").title())

    stats_parts = []
    if trophies is not None:
        stats_parts.append(f"🏆 <b>Кубки/Трофеи:</b> <code>{trophies:,}</code>")
    if town_hall is not None:
        stats_parts.append(f"🏰 <b>Уровень Ратуши (ТХ):</b> <code>{town_hall}</code>")
    if brawlers_count is not None:
        stats_parts.append(f"🥊 <b>Бойцы (Бравлеры):</b> <code>{brawlers_count}</code>")
    stats_block = ("\n".join(stats_parts) + "\n") if stats_parts else ""

    recovery_codes_block = (
        f"🔐 <b>Коды восстановления Supercell ID:</b>\n<code>{html.escape(recovery_codes)}</code>\n"
        if recovery_codes else ""
    )

    mail_access_block = (
        f"📬 <b>Доступ к почте:</b> {'Полный (родная/авторег)' if has_mail_access else 'Без входа в почту'}\n"
    )

    comment_block = f"📝 <b>Инструкция продавца:</b>\n{html.escape(comment)}\n" if comment else ""

    text = tmpl
    text = text.replace("{game_name}", html.escape(game_name))
    text = text.replace("{lot_name}", html.escape(lot_name))
    text = text.replace("{email}", html.escape(email))
    text = text.replace("{password}", html.escape(password))
    text = text.replace("{stats_block}", stats_block)
    text = text.replace("{recovery_codes_block}", recovery_codes_block)
    text = text.replace("{mail_access_block}", mail_access_block)
    text = text.replace("{comment_block}", comment_block)

    return text.strip()


def parse_supercell_bulk_lines(text: str) -> list[dict[str, Any]]:
    """Парсит строки формата email:password[:recovery_codes:comment] или с разделителем пробел/таб/точка с запятой."""
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
            email = parts[0]
            password = parts[1]
            recovery_codes = parts[2] if len(parts) > 2 else None
            comment = parts[3] if len(parts) > 3 else None
            results.append({
                "email": email,
                "password": password,
                "recovery_codes": recovery_codes,
                "comment": comment,
            })
    return results
