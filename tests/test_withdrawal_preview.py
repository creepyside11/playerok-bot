from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app import handlers
from app.states import WithdrawalPreview
from app.withdrawal_preview import (
    PreviewUnavailable, current_withdrawable, parse_details_suffix,
    parse_preview_amount, remaining_balance,
)


@pytest.mark.parametrize("raw,expected", [("500", "500"), ("17,50", "17.50"), ("0.01", "0.01")])
def test_amount(raw, expected):
    assert parse_preview_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["0", "-1", "1e2", "NaN", "1.001", "abc", "", None])
def test_invalid_amount(raw):
    with pytest.raises(ValueError):
        parse_preview_amount(raw)


@pytest.mark.parametrize("raw", ["4111111111111111", "+79991234567", "123", "12345", "１２３４", None])
def test_full_or_invalid_requisites_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_details_suffix(raw)


def test_suffix_and_remainder():
    assert parse_details_suffix("0012") == "0012"
    assert remaining_balance(Decimal("500.55"), Decimal("100.30")) == Decimal("400.25")
    with pytest.raises(ValueError):
        remaining_balance(Decimal("10"), Decimal("11"))


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked,identity,can_read", [(True, "seller", False), (None, "seller", False),
                                                        (False, "other", False), (False, "seller", True)])
async def test_read_only_balance_checks_account_status(blocked, identity, can_read):
    client = NS(viewer=AsyncMock(return_value=NS(id=identity, is_blocked=blocked)),
                balance=AsyncMock(return_value={"withdrawable": "123.45", "value": 1000}))
    if can_read:
        assert await current_withdrawable(client, "seller") == Decimal("123.45")
        client.balance.assert_awaited_once()
    else:
        with pytest.raises(PreviewUnavailable):
            await current_withdrawable(client, "seller")
        client.balance.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawable", [None, "-1", "NaN", "1.001", "1e80"])
async def test_does_not_guess_balance(withdrawable):
    client = NS(viewer=AsyncMock(return_value=NS(id="seller", is_blocked=False)),
                balance=AsyncMock(return_value={"withdrawable": withdrawable, "value": 1000}))
    with pytest.raises(PreviewUnavailable):
        await current_withdrawable(client, "seller")


def mock_callback(data="payout_preview:start", chat=ChatType.PRIVATE):
    return NS(data=data, from_user=NS(id=21), message=NS(chat=NS(type=chat)), answer=AsyncMock())


def mock_message(text):
    return NS(text=text, from_user=NS(id=21), answer=AsyncMock(), delete=AsyncMock())


@pytest.fixture
def preview_setup(monkeypatch):
    account = NS(id=uuid4(), playerok_user_id="seller", tg_user_id=21, username="seller")
    client = NS(viewer=AsyncMock(return_value=NS(id="seller", is_blocked=False)),
                balance=AsyncMock(return_value={"withdrawable": "600.00"}))
    monkeypatch.setattr(handlers, "active_account", AsyncMock(return_value=account))
    monkeypatch.setattr(handlers, "svc", lambda: NS(gateway=NS(get_client=AsyncMock(return_value=client))))
    monkeypatch.setattr(handlers, "edit", AsyncMock())
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=21, user_id=21))
    return state, account, client


@pytest.mark.asyncio
async def test_full_preview_flow_and_replayed_confirmation(preview_setup):
    state, _, client = preview_setup
    await handlers.preview_start(mock_callback(), state)
    assert await state.get_state() == WithdrawalPreview.amount.state
    await handlers.preview_amount(mock_message("700"), state)
    assert await state.get_state() == WithdrawalPreview.amount.state
    await handlers.preview_amount(mock_message("150,50"), state)
    assert await state.get_state() == WithdrawalPreview.last_four.state
    invalid = mock_message("4111111111111111")
    await handlers.preview_details(invalid, state)
    invalid.delete.assert_awaited_once()
    assert "4111111111111111" not in str(await state.get_data())
    assert await state.get_state() == WithdrawalPreview.last_four.state
    await handlers.preview_details(mock_message("0011"), state)
    assert await state.get_state() == WithdrawalPreview.confirm.state
    nonce = (await state.get_data())["preview_nonce"]
    confirm = mock_callback("payout_preview:confirm:" + nonce)

    client.balance.return_value = {"withdrawable": "580.00"}
    await handlers.preview_confirm(confirm, state)
    assert await state.get_state() == WithdrawalPreview.confirm.state
    assert (await state.get_data())["preview_nonce"] != nonce
    assert "429,50" in handlers.edit.call_args.args[1]
    await handlers.preview_confirm(confirm, state)
    assert await state.get_state() == WithdrawalPreview.confirm.state
    new_confirm = mock_callback("payout_preview:confirm:" + (await state.get_data())["preview_nonce"])
    await handlers.preview_confirm(new_confirm, state)
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert "Средства не списаны" in handlers.edit.call_args.args[1]


@pytest.mark.asyncio
async def test_account_switch_invalidates_preview(preview_setup):
    state, _, client = preview_setup
    await handlers.preview_start(mock_callback(), state)
    handlers.active_account.return_value = NS(id=uuid4(), tg_user_id=21, playerok_user_id="different")
    await handlers.preview_amount(mock_message("100"), state)
    assert await state.get_state() is None
    assert client.balance.await_count == 1


@pytest.mark.asyncio
async def test_blocked_and_group_deny_preview(preview_setup):
    state, _, client = preview_setup
    await handlers.preview_start(mock_callback(chat=ChatType.GROUP), state)
    client.viewer.assert_not_awaited()
    client.viewer.return_value = NS(id="seller", is_blocked=True)
    await handlers.preview_start(mock_callback(), state)
    client.balance.assert_not_awaited()
    assert await state.get_state() is None
