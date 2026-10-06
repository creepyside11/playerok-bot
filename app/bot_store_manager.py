from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
import httpx
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, PlayerokAccount

logger = logging.getLogger("bot_store_manager")

GITHUB_SOURCES_URL = "https://github.com/creepyside11/magazinbotsozdatel.git"
DEFAULT_API_URL = "https://host-sait-vercel-api-murex.vercel.app"

BOT_TYPES = [
    ("shop", "🛍 Бот-Магазин + Mini App"),
]


class BotStoreConfig(Base):
    __tablename__ = "bot_store_configs"

    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    api_url: Mapped[str] = mapped_column(String(256), default=DEFAULT_API_URL, nullable=False)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BotStoreLotRule(Base):
    __tablename__ = "bot_store_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    bot_type: Mapped[str] = mapped_column(String(64), default="shop", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_bot_store_lot_rule"),
    )


class BotStoreDeal(Base):
    __tablename__ = "bot_store_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bot_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="waiting_choice", nullable=False)
    # waiting_choice -> waiting_token -> waiting_admin_id -> hosting -> completed
    bot_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hosted_bot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_bot_store_deal"),
    )


async def send_to_hosting_api(api_url: str, api_key: str, name: str, token: str, admin_id: str) -> dict[str, Any]:
    url = f"{api_url.rstrip('/')}/v1/bots"
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "name": name,
        "source": "github",
        "repo": GITHUB_SOURCES_URL,
        "branch": "main",
        "runtime": "node",
        "entrypoint": "index.js",
        "token": token.strip(),
        "env": {
            "ADMIN_ID": admin_id.strip()
        }
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()
