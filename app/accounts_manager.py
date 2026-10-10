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

logger = logging.getLogger("accounts_manager")

DEFAULT_DELIVERY_TEMPLATE = (
    "🎮 <b>Данные вашего аккаунта {game}:</b>\n"
    "📦 Товар: <b>{lot_name}</b>\n\n"
    "🔑 <b>Логин:</b> <code>{login}</code>\n"
    "🔒 <b>Пароль:</b> <code>{password}</code>\n"
    "{id_block}"
    "{server_block}"
    "{extra_block}"
    "\n{comment_block}"
    "🤝 <i>Спасибо за покупку! Проверьте данные и подтвердите получение заказа.</i>"
)

PRESET_GAMES = [
    "Black Russia",
    "Brawl Stars",
    "Standoff 2",
    "Roblox",
    "Genshin Impact",
    "Steam",
    "Telegram",
    "Grand Mobile",
    "Free Fire",
    "PUBG Mobile",
    "Minecraft",
    "Clash of Clans",
]


class UniversalGame(Base):
    """Игра или приложение в каталоге автовыдачи."""
    __tablename__ = "universal_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "title", name="uq_universal_game_title"),
    )


class UniversalAccountStock(Base):
    """Склад аккаунтов для конкретной игры."""
    __tablename__ = "universal_account_stock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    game_id: Mapped[int] = mapped_column(
        ForeignKey("universal_games.id", ondelete="CASCADE"), nullable=False, index=True
    )
    login: Mapped[str] = mapped_column(String(256), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    account_identifier: Mapped[str | None] = mapped_column(String(256), nullable=True)  # ID аккаунта / Ник
    server: Mapped[str | None] = mapped_column(String(128), nullable=True)  # Сервер
    additional_data_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)  # Пин-код / почта / секретка
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)  # Инструкция / коммент к аккаунту
    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False, index=True)
    issued_deal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UniversalAccountLotRule(Base):
    """Привязка лота Playerok к игре для автовыдачи."""
    __tablename__ = "universal_account_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    game_id: Mapped[int] = mapped_column(
        ForeignKey("universal_games.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    delivery_template: Mapped[str] = mapped_column(Text, default=DEFAULT_DELIVERY_TEMPLATE, nullable=False)
    auto_sent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_universal_lot_rule"),
    )


class UniversalAccountDeal(Base):
    """История выданных аккаунтов по сделкам."""
    __tablename__ = "universal_account_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("universal_account_stock.id", ondelete="CASCADE"), nullable=False
    )
    game_id: Mapped[int] = mapped_column(
        ForeignKey("universal_games.id", ondelete="CASCADE"), nullable=False
    )
    login: Mapped[str] = mapped_column(String(256), nullable=False)
    deal_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_universal_acc_deal"),
    )


def format_universal_delivery_message(
    template: str,
    game_title: str,
    lot_name: str,
    login: str,
    password: str,
    account_id: str | None = None,
    server: str | None = None,
    extra: str | None = None,
    comment: str | None = None,
) -> str:
    """Форматирует сообщение покупателю с подстановкой плейсхолдеров."""
    tmpl = template or DEFAULT_DELIVERY_TEMPLATE

    id_block = f"🆔 <b>ID / Никнейм:</b> <code>{html.escape(account_id)}</code>\n" if account_id else ""
    server_block = f"🌐 <b>Сервер:</b> <code>{html.escape(server)}</code>\n" if server else ""
    extra_block = f"ℹ️ <b>Доп. данные:</b> <code>{html.escape(extra)}</code>\n" if extra else ""
    comment_block = f"📝 <b>Инструкция / Комментарий:</b>\n{html.escape(comment)}\n" if comment else ""

    text = tmpl.replace("{game}", html.escape(game_title))
    text = text.replace("{lot_name}", html.escape(lot_name))
    text = text.replace("{login}", html.escape(login))
    text = text.replace("{password}", html.escape(password))
    text = text.replace("{account_id}", html.escape(account_id or "—"))
    text = text.replace("{server}", html.escape(server or "—"))
    text = text.replace("{extra}", html.escape(extra or "—"))
    text = text.replace("{comment}", html.escape(comment or ""))
    text = text.replace("{id_block}", id_block)
    text = text.replace("{server_block}", server_block)
    text = text.replace("{extra_block}", extra_block)
    text = text.replace("{comment_block}", comment_block)
    return text
