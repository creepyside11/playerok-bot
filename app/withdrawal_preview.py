"""Local calculation for an existing account balance; no payment execution."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from .item_publication import amount


class PreviewUnavailable(ValueError):
    """The account or its balance cannot safely be used for a preview."""


def parse_preview_amount(raw: str | None) -> Decimal:
    # Decimal-only input; no scientific notation, sign, or fractional kopecks.
    value = (raw or "").strip()
    if not re.fullmatch(r"[0-9]{1,12}(?:[.,][0-9]{1,2})?", value):
        raise ValueError("Введите сумму в рублях, например 500 или 500,50.")
    result = Decimal(value.replace(",", "."))
    if result <= 0:
        raise ValueError("Сумма должна быть больше нуля.")
    return result


def parse_details_suffix(raw: str | None) -> str:
    value = (raw or "").strip()
    if not re.fullmatch(r"[0-9]{4}", value):
        raise ValueError("Введите ровно 4 последние цифры. Полные реквизиты не отправляйте.")
    return value


def format_money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f").replace(".", ",") + " ₽"


async def current_withdrawable(client: Any, expected_id: str) -> Decimal:
    """Only read the current account and explicitly reported withdrawable balance."""
    viewer = await client.viewer()
    if not expected_id or str(getattr(viewer, "id", "")) != str(expected_id):
        raise PreviewUnavailable("Не удалось подтвердить подключённый аккаунт.")
    if getattr(viewer, "is_blocked", None) is not False:
        raise PreviewUnavailable("Аккаунт заблокирован или его статус неизвестен. Обратитесь в Playerok.")
    balance = await client.balance()
    value = amount(balance.get("withdrawable")) if isinstance(balance, dict) else None
    if value is None or value < 0 or value > Decimal("999999999999.99") or value != value.quantize(Decimal("0.01")):
        raise PreviewUnavailable("Playerok не вернул корректную доступную к выводу сумму.")
    return value.quantize(Decimal("0.01"))


def remaining_balance(withdrawable: Decimal, requested: Decimal) -> Decimal:
    if requested <= 0 or requested > withdrawable:
        raise ValueError("Сумма больше доступного к выводу остатка или равна нулю.")
    return withdrawable - requested
