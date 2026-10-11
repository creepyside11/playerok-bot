from __future__ import annotations

import asyncio
import logging
from typing import Any
import urllib.parse

import httpx
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

logger = logging.getLogger("autosmm_manager")


class AutoSmmConfig(Base):
    """Настройки подключения к SMM-панели для конкретного аккаунта Playerok."""
    __tablename__ = "autosmm_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    api_url: Mapped[str] = mapped_column(String(256), default="https://smmway.com/api/v2", nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_sent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_seller: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AutoSmmLotRule(Base):
    """Привязка лота Playerok к услуге в SMM-панели."""
    __tablename__ = "autosmm_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    service_id: Mapped[str] = mapped_column(String(64), nullable=False)
    service_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    auto_sent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_autosmm_lot_rule"),
    )


class AutoSmmOrder(Base):
    """История созданных заказов в SMM-панели."""
    __tablename__ = "autosmm_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    smm_order_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    service_id: Mapped[str] = mapped_column(String(64), nullable=False)
    service_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    link: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    charge: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="Pending", nullable=False)
    deal_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_autosmm_order"),
    )


class SmmApiClient:
    """Клиент для стандартного SMM API v2 (smmway, justanotherpanel и др.)."""

    def __init__(self, api_url: str, api_key: str):
        self.api_url = (api_url or "https://smmway.com/api/v2").strip()
        self.api_key = (api_key or "").strip()

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = {"key": self.api_key, **payload}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.api_url, data=data)
            if resp.status_code >= 400:
                raise RuntimeError(f"SMM API HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    async def get_balance(self) -> dict[str, Any]:
        """Возвращает баланс и валюту."""
        return await self._post({"action": "balance"})

    async def get_services(self) -> list[dict[str, Any]]:
        """Возвращает список всех услуг провайдера."""
        res = await self._post({"action": "services"})
        return list(res or []) if isinstance(res, list) else []

    async def create_order(self, service_id: str | int, link: str, quantity: int) -> dict[str, Any]:
        """Создает заказ на накрутку."""
        return await self._post({
            "action": "add",
            "service": str(service_id),
            "link": link,
            "quantity": int(quantity),
        })

    async def get_order_status(self, order_id: str | int) -> dict[str, Any]:
        """Получает статус заказа."""
        return await self._post({"action": "status", "order": str(order_id)})
