from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from playerokapi.enums import TransactionOperations, TransactionStatuses
from app.withdrawal_history import HISTORY_LIMIT, masked_details, payout_history


def transaction(*, owner="seller", operation=TransactionOperations.WITHDRAW,
                status=TransactionStatuses.PROCESSING, details="4111 1111 1111 1111"):
    return NS(
        operation=operation, user=NS(id=owner),
        status=status, value="500.00", fee=60, created_at="2026-11-25T12:00:00Z",
        props=NS(payment_account=NS(value=details), user_data=NS(email="private@example.org")),
    )


@pytest.mark.asyncio
async def test_payout_history_only_returns_own_withdrawals_and_masked_details():
    client = NS(call=AsyncMock(return_value=NS(
        transactions=[
            transaction(),
            transaction(owner="foreign"),
            transaction(operation=TransactionOperations.DEPOSIT),
            transaction(status="UNKNOWN_NEW_STATUS", details="private@example.org"),
            None,
        ],
        page_info=NS(has_next_page=True, end_cursor="next-page"),
    )))
    result = await payout_history(client, "seller", after_cursor="first-page")
    client.call.assert_awaited_once_with(
        "get_transactions", operation=TransactionOperations.WITHDRAW,
        count=HISTORY_LIMIT, after_cursor="first-page",
    )
    assert result["cursor"] == "next-page"
    assert len(result["items"]) == 2
    assert result["items"][0] == {
        "amount": "500.00", "fee": "60", "status": "PROCESSING",
        "statusLabel": "В обработке", "time": "2026-11-25T12:00:00Z", "details": "•••• 1111",
    }
    assert result["items"][1]["status"] == "UNKNOWN"
    assert result["items"][1]["details"] is None
    assert "4111" not in str(result)
    assert "private@example.org" not in str(result)


@pytest.mark.parametrize("details, expected", [
    ("+7 (999) 123-45-67", "•••• 4567"),
    ("**** 3050", "•••• 3050"),
    ("not-an-account-1000", None),
    ("123", None),
    ("", None),
    (None, None),
])
def test_masked_details(details, expected):
    assert masked_details(transaction(details=details)) == expected


@pytest.mark.asyncio
async def test_unavailable_history_is_not_reported_as_empty():
    with pytest.raises(ValueError):
        await payout_history(NS(call=AsyncMock(return_value=None)), "seller")
