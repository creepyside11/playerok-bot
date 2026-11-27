"""Read-only, privacy-preserving view of existing Playerok payouts."""

from __future__ import annotations

import re
from typing import Any

from playerokapi.enums import TransactionOperations

from .item_publication import amount


PLAYEROK_URL = "https://playerok.com/"
PLAYEROK_CONTACTS_URL = "https://playerok.com/contacts"
HISTORY_LIMIT = 10
STATUS_LABELS = {
    "PENDING": "Ожидает обработки",
    "PROCESSING": "В обработке",
    "CONFIRMED": "Подтверждён",
    "ROLLED_BACK": "Возврат",
    "FAILED": "Ошибка",
}


def enum_name(value: Any) -> str:
    return str(getattr(value, "name", value) or "")


def masked_details(transaction: Any) -> str | None:
    """Never return a complete card, phone number, account, or wallet address."""
    props = getattr(transaction, "props", None)
    payment_account = getattr(props, "payment_account", None)
    user_data = getattr(props, "user_data", None)
    for value in (
        getattr(payment_account, "value", None),
        getattr(user_data, "account", None),
        getattr(user_data, "phone_number", None),
    ):
        if not isinstance(value, str):
            continue
        # Only accept recognizable numeric or masked numeric account details.
        compact = re.sub(r"[\s()+-]", "", value)
        if re.fullmatch(r"[0-9*•●xX]{4,40}", compact) and re.search(r"[0-9]{4}$", compact):
            return "•••• " + compact[-4:]
    return None


def payout_json(transaction: Any) -> dict[str, Any]:
    status = enum_name(getattr(transaction, "status", None))
    value = amount(getattr(transaction, "value", None))
    fee = amount(getattr(transaction, "fee", None))
    created = getattr(transaction, "created_at", None)
    if hasattr(created, "isoformat"):
        created = created.isoformat()
    return {
        "amount": format(value, "f") if value is not None else None,
        "fee": format(fee, "f") if fee is not None else None,
        "status": status if status in STATUS_LABELS else "UNKNOWN",
        "statusLabel": STATUS_LABELS.get(status, "Статус неизвестен"),
        "time": created if isinstance(created, str) else None,
        "details": masked_details(transaction),
    }


async def payout_history(client: Any, own_id: str, *, after_cursor: str | None = None) -> dict[str, Any]:
    """Load recent existing withdrawals; never requests or retries a payout."""
    page = await client.call(
        "get_transactions",
        operation=TransactionOperations.WITHDRAW,
        count=HISTORY_LIMIT,
        after_cursor=after_cursor,
    )
    transactions = getattr(page, "transactions", None)
    if not isinstance(transactions, (list, tuple)):
        raise ValueError("Playerok did not return transaction history")
    items = [
        payout_json(transaction)
        for transaction in transactions
        if transaction is not None
        and enum_name(getattr(transaction, "operation", None)) == "WITHDRAW"
        and str(getattr(getattr(transaction, "user", None), "id", "")) == str(own_id)
    ]
    info = getattr(page, "page_info", None)
    return {
        "items": items,
        "cursor": getattr(info, "end_cursor", None) if getattr(info, "has_next_page", False) else None,
    }
