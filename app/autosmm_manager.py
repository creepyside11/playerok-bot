from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

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
    update,
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
    keyword: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
    provider_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    refill_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    refill_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refill_requested_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_autosmm_order"),
    )


DEFAULT_API_URL = "https://smmway.com/api/v2"
REFILL_TERMINAL = {"completed", "rejected", "canceled", "cancelled", "error", "failed"}
REFILL_ELIGIBLE_ORDER = {"completed"}


class SmmError(ValueError):
    pass


def normalized_keyword(value: str) -> str:
    return " ".join(value.casefold().split())


def valid_keyword(value: Any) -> bool:
    return isinstance(value, str) and 2 <= len(normalized_keyword(value)) <= 128 and any(c.isalnum() for c in value)


def keyword_lot_id(value: str) -> str:
    return "keyword:" + hashlib.sha256(normalized_keyword(value).encode()).hexdigest()


def keyword_matches(keyword: str, title: str) -> bool:
    keyword, title = normalized_keyword(keyword), normalized_keyword(title)
    if not keyword:
        return False
    pattern = re.escape(keyword)
    if keyword[0].isalnum():
        pattern = r"(?<!\w)" + pattern
    if keyword[-1].isalnum():
        pattern += r"(?!\w)"
    return re.search(pattern, title) is not None


async def resolve_lot_rule(session: Any, account_id: Any, item_id: str, title: str, slug: str = "") -> AutoSmmLotRule | None:
    rows = list(await session.scalars(select(AutoSmmLotRule).where(AutoSmmLotRule.account_id == account_id)))
    direct = next((row for row in rows if not row.keyword and row.lot_id == item_id), None)
    if direct is not None:
        return direct if direct.enabled else None
    if slug:
        direct = next((row for row in rows if not row.keyword and row.lot_id == slug), None)
        if direct is not None:
            return direct if direct.enabled else None
    matches = [row for row in rows if row.enabled and row.keyword and keyword_matches(row.keyword, title)]
    if matches:
        length = max(len(normalized_keyword(row.keyword)) for row in matches)
        matches = [row for row in matches if len(normalized_keyword(row.keyword)) == length]
        # An equal-priority conflict must not dispatch a different service by chance.
        if len({(row.service_id, row.quantity) for row in matches}) != 1:
            return None
        return min(matches, key=lambda row: row.id)
    # Retain explicitly stored legacy wildcard rules, but never infer a rule
    # from a different exact-ID lot's title.
    return next((row for row in rows if row.enabled and not row.keyword and row.lot_id == "*"), None)


def service_refill_allowed(service: dict[str, Any]) -> bool:
    value = service.get("refill")
    return value is True or type(value) is int and value == 1 or isinstance(value, str) and value.strip().casefold() in {"true", "yes", "1"}


def provider_service(services: list[dict[str, Any]], service_id: str) -> dict[str, Any] | None:
    return next((row for row in services if str(row.get("service") or row.get("id") or "") == service_id), None)


def valid_service_id(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value))


def provider_id(value: Any) -> str | None:
    if type(value) not in (str, int):
        return None
    value = str(value).strip()
    return value if value != "0" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) else None


def validate_quantity(quantity: Any, service: dict[str, Any]) -> int:
    if type(quantity) is not int or not 1 <= quantity <= 10_000_000:
        raise SmmError("Введите допустимое целое количество.")
    for field, compare, label in (("min", lambda x: quantity < x, "Минимум"), ("max", lambda x: quantity > x, "Максимум")):
        try:
            limit = int(service.get(field))
        except (ValueError, TypeError):
            continue
        if compare(limit):
            raise SmmError(f"{label} для услуги: {limit}.")
    return quantity


async def bind_service(db: Any, account_id: Any, *, service: dict[str, Any], quantity: int,
                       keyword: str | None = None, lot_id: str | None = None, lot_title: str = "") -> AutoSmmLotRule:
    service_id = str(service.get("service") or service.get("id") or "")
    if not valid_service_id(service_id):
        raise SmmError("Услуга не найдена в каталоге.")
    quantity = validate_quantity(quantity, service)
    if keyword is not None:
        if lot_id is not None or not valid_keyword(keyword):
            raise SmmError("Введите слово или фразу от 2 до 128 символов.")
        keyword = " ".join(keyword.split())
        lot_id, lot_title = keyword_lot_id(keyword), keyword
    elif not isinstance(lot_id, str) or not lot_id or lot_id == "*" or lot_id.startswith("keyword:") or len(lot_id) > 128:
        raise SmmError("Выберите конкретный лот.")
    from .models import PlayerokAccount
    async with db() as session:
        await session.get(PlayerokAccount, account_id, with_for_update=True)
        rule = await session.scalar(select(AutoSmmLotRule).where(
            AutoSmmLotRule.account_id == account_id, AutoSmmLotRule.lot_id == lot_id,
        ))
        if rule is None:
            rule = AutoSmmLotRule(account_id=account_id, lot_id=lot_id, lot_title=lot_title[:256], enabled=True)
            session.add(rule)
        rule.keyword, rule.lot_title = keyword, lot_title[:256]
        rule.service_id, rule.service_name, rule.quantity = service_id, str(service.get("name") or service_id)[:256], quantity
        await session.commit()
        return rule


def normalize_api_url(value: str) -> str:
    url = str(value or DEFAULT_API_URL).strip()
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SmmError("Некорректный адрес API.") from exc
    if (len(url) > 256 or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url
            or parsed.scheme != "https" or not host or
            parsed.username or parsed.password or parsed.query or parsed.fragment or port not in (None, 443) or
            "." not in host or host.casefold().endswith((".local", ".localhost", ".internal"))):
        raise SmmError("Укажите публичный HTTPS-адрес API без ключей в URL.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise SmmError("Адрес API должен быть публичным.")
    return url


async def check_public_host(url: str) -> None:
    host = urlsplit(url).hostname
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SmmError("Не удалось найти адрес SMM-панели.") from exc
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise SmmError("Адрес API должен быть публичным.")


class SmmApiClient:
    """Клиент стандартного API v2; не повторяет запросы с побочными эффектами."""

    def __init__(self, api_url: str, api_key: str):
        self.api_url = normalize_api_url(api_url)
        self.api_key = (api_key or "").strip()

    async def _post(self, payload: dict[str, Any]) -> Any:
        await check_public_host(self.api_url)
        data = {"key": self.api_key, **payload}
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=False) as client:
                resp = await client.post(self.api_url, data=data)
            if not 200 <= resp.status_code < 300:
                raise SmmError(f"SMM API HTTP {resp.status_code}.")
            result = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, SmmError):
                raise
            raise SmmError("Не удалось получить ответ SMM-панели.") from exc
        if isinstance(result, dict) and result.get("error"):
            # Provider responses sometimes include arbitrary data. Do not log
            # or disclose an API key, URL parameters, or request payload.
            error = str(result["error"])
            if self.api_key:
                error = error.replace(self.api_key, "[скрыто]")
            return {"error": error[:250]}
        return result

    async def _object(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self._post(payload)
        if not isinstance(result, dict):
            raise SmmError("Панель вернула неожиданный формат ответа.")
        return result

    async def get_balance(self) -> dict[str, Any]:
        result = await self._object({"action": "balance"})
        if result.get("error") or "balance" not in result:
            raise SmmError(str(result.get("error") or "Баланс недоступен."))
        return result

    async def get_services(self) -> list[dict[str, Any]]:
        result = await self._post({"action": "services"})
        if not isinstance(result, list):
            raise SmmError(str(result.get("error") if isinstance(result, dict) else "") or "Каталог недоступен.")
        return [row for row in result if isinstance(row, dict)]

    async def create_order(self, service_id: str | int, link: str, quantity: int) -> dict[str, Any]:
        return await self._object({"action": "add", "service": str(service_id), "link": link, "quantity": int(quantity)})

    async def get_order_status(self, order_id: str | int) -> dict[str, Any]:
        return await self._object({"action": "status", "order": str(order_id)})

    async def request_refill(self, order_id: str) -> dict[str, Any]:
        return await self._object({"action": "refill", "order": str(order_id)})

    async def get_refill_status(self, refill_id: str) -> dict[str, Any]:
        return await self._object({"action": "refill_status", "refill": str(refill_id)})


async def get_configured_client(db: Any, cipher: Any, account_id: Any) -> tuple[SmmApiClient | None, AutoSmmConfig | None]:
    async with db() as session:
        cfg = await session.scalar(select(AutoSmmConfig).where(AutoSmmConfig.account_id == account_id))
    if not cfg or not cfg.api_key_encrypted:
        return None, cfg
    if cipher is None:
        raise SmmError("Шифрование API-ключа не настроено.")
    try:
        key = cipher.decrypt(cfg.api_key_encrypted)
    except Exception as exc:
        raise SmmError("Не удалось прочитать API-ключ. Сохраните ключ заново.") from exc
    if not key:
        raise SmmError("Сохраните API-ключ SMM-панели заново.")
    return SmmApiClient(cfg.api_url, key), cfg


def refill_pending(order: AutoSmmOrder) -> bool:
    return bool(order.refill_status and order.refill_status.casefold() not in REFILL_TERMINAL)


async def get_owned_order(db: Any, account_id: Any, order_id: int) -> AutoSmmOrder:
    async with db() as session:
        order = await session.get(AutoSmmOrder, order_id)
    if order is None or order.account_id != account_id:
        raise SmmError("Заказ не найден.")
    return order


def ensure_order_provider(order: AutoSmmOrder, client: Any) -> None:
    if order.provider_url and order.provider_url != client.api_url:
        raise SmmError("Заказ относится к другой SMM-панели. Верните прежние настройки подключения.")
    if not order.smm_order_id or order.smm_order_id == "0":
        raise SmmError("У заказа нет подтверждённого ID панели.")


async def refresh_order(db: Any, account_id: Any, order_id: int, client: Any) -> tuple[AutoSmmOrder, bool]:
    order = await get_owned_order(db, account_id, order_id)
    ensure_order_provider(order, client)
    status = await client.get_order_status(order.smm_order_id)
    if status.get("error") or not status.get("status"):
        raise SmmError(str(status.get("error") or "Панель не вернула статус заказа."))
    refill_status = None
    if order.refill_id:
        try:
            answer = await client.get_refill_status(order.refill_id)
            if not answer.get("error") and answer.get("status"):
                refill_status = str(answer["status"])[:64]
        except SmmError:
            pass
    async with db() as session:
        current = await session.get(AutoSmmOrder, order.id, with_for_update=True)
        if not current or current.account_id != account_id:
            raise SmmError("Заказ не найден.")
        current.status = str(status["status"])[:64]
        # Never overwrite a newer request while another screen is refreshing.
        if refill_status and current.refill_id == order.refill_id and current.refill_status != "Requesting":
            current.refill_status = refill_status
        await session.commit()
        order = current
    try:
        service = provider_service(await client.get_services(), order.service_id)
    except SmmError:
        service = None
    available = bool(service and service_refill_allowed(service) and
                     order.status.casefold() in REFILL_ELIGIBLE_ORDER and not refill_pending(order))
    return order, available


async def request_order_refill(db: Any, account_id: Any, order_id: int, client: Any, *, confirm_legacy: bool = False) -> AutoSmmOrder:
    order = await get_owned_order(db, account_id, order_id)
    ensure_order_provider(order, client)
    if not order.provider_url and not confirm_legacy:
        raise SmmError("Подтвердите, что старый заказ относится к текущей панели.")
    if refill_pending(order):
        raise SmmError("Уже есть незавершённый запрос refill. Обновите его статус.")
    order, available = await refresh_order(db, account_id, order_id, client)
    if not available:
        raise SmmError("Refill сейчас недоступен для этой услуги или статуса заказа.")
    async with db() as session:
        result = await session.execute(update(AutoSmmOrder).where(
            AutoSmmOrder.id == order.id, AutoSmmOrder.account_id == account_id,
            AutoSmmOrder.refill_id == order.refill_id, AutoSmmOrder.refill_status == order.refill_status,
        ).values(refill_status="Requesting", refill_id=None, provider_url=client.api_url,
                 refill_requested_at=datetime.now(timezone.utc)))
        if result.rowcount != 1:
            raise SmmError("Запрос уже изменился. Обновите заказ.")
        await session.commit()
    try:
        response = await client.request_refill(order.smm_order_id)
        if response.get("error"):
            new_status, external_id = "Rejected", None
            error = str(response["error"])[:250]
        else:
            external_id = provider_id(response.get("refill"))
            if not external_id:
                raise SmmError("Панель не вернула ID refill.")
            new_status, error = "Pending", None
    except Exception:
        # Transport errors can occur after the provider accepted a request.
        # Mark uncertainty and require manual reconciliation instead of retrying.
        new_status, external_id, error = "Unknown", None, "Результат запроса неизвестен. Проверьте его в SMM-панели, не отправляйте повторно."
    async with db() as session:
        current = await session.get(AutoSmmOrder, order.id, with_for_update=True)
        current.refill_id, current.refill_status = external_id, new_status
        await session.commit()
    if error:
        raise SmmError(error)
    return current
