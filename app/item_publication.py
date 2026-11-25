from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from playerokapi.exceptions import RequestFailedError, RequestPlayerokError, RequestSendingError


@dataclass(frozen=True)
class PublicationTariff:
    id: str
    name: str
    fee: Decimal


class PublicationChanged(Exception):
    """Playerok changed the price, balance or available options."""


class PublicationUnconfirmed(Exception):
    def __init__(self, item: Any, failure: Exception | None = None):
        self.item = item
        self.failure = failure


def publication_confirmed(item: Any) -> bool:
    status = getattr(item, "status", None)
    return getattr(status, "name", status) in {"APPROVED", "PENDING_APPROVAL", "PENDING_MODERATION"}


def amount(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def rubles(value: Decimal | int) -> str:
    return format(Decimal(value).normalize(), "f").replace(".", ",") + " ₽"


def tariff_price(item: Any) -> int:
    raw_price = getattr(item, "raw_price", None)
    return int(raw_price if raw_price is not None else item.price)


def parse_discount_price(text: str, base_price: int) -> int:
    if text in {"-", "—"}:
        return base_price
    price = int("".join(text.split()).removesuffix("₽"))
    if not 0 < price <= base_price * 95 // 100:
        raise ValueError("Скидка должна составлять минимум 5%")
    return price


def can_publish(item: Any) -> bool:
    status = getattr(item, "status", None)
    return getattr(item, "may_be_published", None) is not False and getattr(
        status, "name", status
    ) in {"DRAFT", "SOLD", "EXPIRED", "DECLINED"}


async def publication_tariffs(client: Any, item_id: str, price: int) -> list[PublicationTariff]:
    tariffs = []
    for status in await client.get_item_priority_statuses(item_id, price):
        if status is None or not status.id:
            continue
        fee = amount(status.price)
        if fee is not None and fee >= 0:
            tariffs.append(PublicationTariff(str(status.id), str(status.name or "Премиум"), fee))
    return sorted(tariffs, key=lambda tariff: tariff.fee)


async def publication_balance(client: Any) -> Decimal | None:
    try:
        balance = await client.balance()
        available = amount(balance.get("available")) if isinstance(balance, dict) else None
        if available is not None:
            return available
    except Exception:
        pass
    try:
        return amount((await client.viewer()).available_balance)
    except Exception:
        return None


async def confirm_publication(
    client: Any, item_id: str, prices: tuple[int, int], tariff: PublicationTariff, balance: Decimal | None
) -> Any:
    item = await client.get_item(item_id)
    if not can_publish(item) or (int(item.price), tariff_price(item)) != prices:
        raise PublicationChanged()
    tariffs = await publication_tariffs(client, item_id, prices[1])
    if not any(option.id == tariff.id and option.fee == tariff.fee for option in tariffs):
        raise PublicationChanged()
    if tariff.fee > 0:
        current_balance = await publication_balance(client)
        if current_balance is None or current_balance < tariff.fee or current_balance != balance:
            raise PublicationChanged()
    result = None
    failure = None
    try:
        result = await client.publish_item(item_id, tariff.id)
    except RequestPlayerokError as exc:
        if not exc.json.get("data"):
            raise
        failure = exc
    except (RequestFailedError, RequestSendingError, KeyError, TypeError, ValueError) as exc:
        # A response error can arrive after the marketplace has accepted the listing.
        failure = exc

    if publication_confirmed(result):
        return result
    try:
        current = await client.get_item(item_id)
    except Exception:
        current = result
    if publication_confirmed(current):
        return current
    raise PublicationUnconfirmed(current or result, failure)
