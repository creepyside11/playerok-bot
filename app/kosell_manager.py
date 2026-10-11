from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

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

logger = logging.getLogger("kosell_manager")

KOSELL_API_BASE = "https://kosell.store"

DEFAULT_KOSELL_DELIVERY_TEMPLATE = (
    "🎮 <b>Ваша аренда Steam ({product_name}) готова!</b>\n"
    "📦 Товар: <b>{lot_name}</b>\n"
    "⏱ Время аренды: <b>{duration_hours} ч.</b> (до {expires_at})\n\n"
    "👤 <b>Логин Steam:</b> <code>{steam_login}</code>\n"
    "🔑 <b>Пароль Steam:</b> <code>{steam_password}</code>\n\n"
    "🔐 <b>Код Steam Guard (2FA):</b>\n"
    "Для получения свежего кода подтверждения входа отправьте в этот чат команду:\n"
    "<code>#код</code> или <code>#guard</code>\n\n"
    "📋 <b>Инструкция по входу в Steam:</b>\n"
    "1. Откройте приложение Steam на ПК или мобильном устройстве.\n"
    "2. Введите указанный логин и пароль.\n"
    "3. Когда Steam запросит код подтверждения — напишите в этот чат <code>#код</code>.\n"
    "4. Введите полученный код и играйте!\n\n"
    "⚠️ <i>Не меняйте пароль и данные аккаунта. Играйте только в рамках оплаченного времени.</i>\n"
    "🤝 <i>Спасибо за покупку! Пожалуйста, подтвердите получение заказа.</i>"
)


class KosellAccountConfig(Base):
    """Настройки подключения к API KOSell для конкретного аккаунта Playerok."""
    __tablename__ = "kosell_account_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="RUB", nullable=False)  # RUB / USD
    auto_sent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_seller: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class KosellLotRule(Base):
    """Привязка лотов Playerok к товарам KOSell."""
    __tablename__ = "kosell_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False)
    duration_hours: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    delivery_template: Mapped[str] = mapped_column(Text, default=DEFAULT_KOSELL_DELIVERY_TEMPLATE, nullable=False)
    auto_sent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_kosell_lot_rule"),
    )


class KosellDeal(Base):
    """История созданных аренд KOSell для сделок Playerok."""
    __tablename__ = "kosell_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    rental_uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False)
    steam_login: Mapped[str] = mapped_column(String(128), nullable=False)
    duration_hours: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    price_paid: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="RUB", nullable=False)
    expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    deal_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    code_requests_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_code_sent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_kosell_deal"),
    )


class KosellApiError(Exception):
    def __init__(self, message: str, status_code: int = 400, raw_error: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.raw_error = raw_error


class KosellClient:
    """Асинхронный клиент для работы с KOSell Public API v1.1.0."""

    def __init__(self, api_key: str, base_url: str = KOSELL_API_BASE, client: httpx.AsyncClient | None = None):
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self._external_client = client

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "Playerok-Bot-Kosell/1.0",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)[:128]
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._headers(idempotency_key=idempotency_key)

        async def _do(cli: httpx.AsyncClient) -> Any:
            resp = await cli.request(method, url, headers=headers, params=params, json=json_data, timeout=30.0)
            if resp.status_code >= 400:
                err_msg = resp.text
                raw_err = None
                try:
                    data = resp.json()
                    err_msg = data.get("message") or data.get("error") or str(data.get("detail")) or resp.text
                    raw_err = data.get("error")
                except Exception:
                    pass
                raise KosellApiError(f"KOSell API {resp.status_code}: {err_msg}", status_code=resp.status_code, raw_error=raw_err)
            return resp.json()

        if self._external_client:
            return await _do(self._external_client)
        async with httpx.AsyncClient() as cli:
            return await _do(cli)

    async def get_balance(self) -> dict[str, Any]:
        """Возвращает баланс в USD и RUB."""
        return await self._request("GET", "/api/v1/account/balance")

    async def get_products(self, search: str | None = None, currency: str = "RUB") -> list[dict[str, Any]]:
        """Возвращает список доступных для аренды игр с ценами и количеством аккаунтов."""
        params: dict[str, Any] = {"currency": currency}
        if search:
            params["search"] = search
        data = await self._request("GET", "/api/v1/rental/products", params=params)
        return list(data or [])

    async def calculate_price(self, product_id: int, hours: int) -> dict[str, Any]:
        """Рассчитывает стоимость аренды игры на указанное количество часов."""
        return await self._request(
            "POST",
            "/api/v1/rental/calculate-price",
            json_data={"product_id": int(product_id), "hours": int(hours)},
        )

    async def create_rental(
        self,
        product_id: int,
        duration_hours: int = 1,
        currency: str = "RUB",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Создает и оплачивает аренду с баланса KOSell."""
        body = {
            "product_id": int(product_id),
            "duration_hours": int(duration_hours),
            "currency": currency.upper(),
        }
        if idempotency_key:
            body["idempotency_key"] = str(idempotency_key)[:128]
        return await self._request(
            "POST",
            "/api/v1/rental/rent",
            json_data=body,
            idempotency_key=idempotency_key,
        )

    async def get_active_rentals(self) -> list[dict[str, Any]]:
        """Возвращает список всех активных аренд."""
        data = await self._request("GET", "/api/v1/rental/active")
        return list(data or [])

    async def get_credentials(self, rental_uid: str) -> dict[str, str]:
        """Получает логин и пароль от арендованного Steam-аккаунта."""
        return await self._request("GET", f"/api/v1/rental/{rental_uid}/credentials")

    async def get_steam_code(self, rental_uid: str) -> dict[str, Any]:
        """Получает текущий код Steam Guard (2FA) для входа."""
        return await self._request("GET", f"/api/v1/rental/{rental_uid}/code")

    async def extend_rental(self, rental_uid: str, hours: int, currency: str = "RUB") -> dict[str, Any]:
        """Продлевает активную аренду."""
        return await self._request(
            "POST",
            f"/api/v1/rental/{rental_uid}/extend",
            json_data={"hours": int(hours), "currency": currency.upper()},
        )

    async def terminate_rental(self, rental_uid: str) -> dict[str, Any]:
        """Досрочно завершает аренду и сбрасывает сессии аккаунта."""
        return await self._request("POST", f"/api/v1/rental/{rental_uid}/terminate")


def format_kosell_delivery_message(
    template: str,
    lot_name: str,
    product_name: str,
    duration_hours: int,
    steam_login: str,
    steam_password: str,
    expires_at: str,
) -> str:
    """Формирует текст автовыдачи для покупателя в чат сделки на Playerok."""
    tmpl = template or DEFAULT_KOSELL_DELIVERY_TEMPLATE
    return (
        tmpl.replace("{lot_name}", lot_name)
        .replace("{product_name}", product_name)
        .replace("{duration_hours}", str(duration_hours))
        .replace("{steam_login}", steam_login)
        .replace("{steam_password}", steam_password)
        .replace("{expires_at}", expires_at)
    )


def format_kosell_code_reply(code: str, expires_in: int | None = None) -> str:
    """Формирует ответ на запрос команды #код или #guard в чате Playerok."""
    rem_str = f"\n⏳ Код действует ещё: {expires_in} сек." if expires_in else ""
    return (
        f"🔐 <b>Ваш код Steam Guard:</b> <code>{code}</code>{rem_str}\n\n"
        f"Введите его в окне авторизации Steam для завершения входа.\n"
        f"<i>Если код устареет, отправьте команду <code>#код</code> ещё раз.</i>"
    )
