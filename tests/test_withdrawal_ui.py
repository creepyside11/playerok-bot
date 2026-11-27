from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from playerokapi.enums import TransactionPaymentMethodIds, TransactionProviderIds
from app import withdrawal_ui
from app.states import WithdrawalCreate
from app.withdrawal_ui import confirmation_text

CARD_ID = "0f3c8f5e-1f4b-4c47-9d5e-2b6f4a1c9d11"

def provider(identifier=TransactionProviderIds.BANK_CARD_RU, *, fee=6,
             methods=(TransactionPaymentMethodIds.MIR,), name="Банковская карта"):
    return NS(
        id=identifier, name=name, fee=fee, min_fee_amount=None, description=None,
        limits=NS(outgoing=NS(min=None, max=None)),
        payment_methods=[NS(id=method, name=method.name, fee=None) for method in methods],
    )

def make_client(*, providers=None, cards=None, banks=None, withdrawable="600.00", blocked=False):
    providers = [provider()] if providers is None else providers
    cards = [NS(id=CARD_ID, card_first_six="411111", card_last_four="1111",
                card_type="MIR", is_chosen=True)] if cards is None else cards
    banks = [NS(id="sber", name="Сбербанк")] if banks is None else banks
    created = NS(
        id="tx-1", operation="WITHDRAW", status="PROCESSING", value="500.00", fee=30, created_at=None,
        props=NS(payment_account=NS(value="4111111111111111"), user_data=None),
    )

    async def call(method, *args, **kwargs):
        if method == "get_transaction_providers":
            return providers
        if method == "get_verified_cards":
            return NS(bank_cards=cards)
        if method == "get_sbp_bank_members":
            return banks
        if method == "request_withdrawal":
            return created
        raise AssertionError(method)

    return NS(
        call=AsyncMock(side_effect=call),
        viewer=AsyncMock(return_value=NS(id="seller", is_blocked=blocked)),
        funds_protection=AsyncMock(return_value=False),
        balance=AsyncMock(return_value={"withdrawable": withdrawable}),
    )

def use_client(client):
    withdrawal_ui.svc = lambda: NS(gateway=NS(get_client=AsyncMock(return_value=client)))

def mock_callback(data="wd:start", chat=ChatType.PRIVATE):
    return NS(data=data, from_user=NS(id=21), message=NS(chat=NS(type=chat)), answer=AsyncMock())

def mock_message(text):
    return NS(text=text, from_user=NS(id=21), answer=AsyncMock(), delete=AsyncMock())

def keyboard_buttons(call):
    return [button for row in call.args[2].inline_keyboard for button in row]

def payout_calls(client):
    return [call for call in client.call.await_args_list if call.args and call.args[0] == "request_withdrawal"]

@pytest.fixture
def setup(monkeypatch):
    account = NS(id=uuid4(), playerok_user_id="seller", tg_user_id=21, username="seller")
    client = make_client()
    monkeypatch.setattr(withdrawal_ui, "active_account", AsyncMock(return_value=account))
    monkeypatch.setattr(withdrawal_ui, "edit", AsyncMock())
    use_client(client)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=21, user_id=21))
    return state, account, client

@pytest.mark.asyncio
async def test_card_payout_flow_creates_payout_once(setup):
    state, _, client = setup
    await withdrawal_ui.start(mock_callback(), state)
    assert await state.get_state() == WithdrawalCreate.confirm.state
    assert "Новая выплата" in withdrawal_ui.edit.call_args.args[1]

    await withdrawal_ui.choose_provider(mock_callback("wd:provider:BANK_CARD_RU"), state)
    assert (await state.get_data())["wd_kind"] == "card_id"
    assert any(button.callback_data == f"wd:card:{CARD_ID}" for button in keyboard_buttons(withdrawal_ui.edit.call_args))

    await withdrawal_ui.choose_card(mock_callback(f"wd:card:{CARD_ID}"), state)
    assert await state.get_state() == WithdrawalCreate.amount.state
    assert (await state.get_data())["wd_requisites"] == CARD_ID

    await withdrawal_ui.input_amount(mock_message("500"), state)
    assert await state.get_state() == WithdrawalCreate.confirm.state
    data = await state.get_data()
    nonce = data["wd_nonce"]
    text = confirmation_text(data, 500, Decimal("600.00"))
    assert "470,00 ₽" in text and "30,00 ₽" in text
    assert "4111111111111111" not in text

    await withdrawal_ui.confirm(mock_callback("wd:confirm:" + nonce), state)
    assert await state.get_state() is None
    assert await state.get_data() == {}
    client.call.assert_awaited_with(
        "request_withdrawal", provider=TransactionProviderIds.BANK_CARD_RU, account=CARD_ID,
        value=500, payment_method_id=TransactionPaymentMethodIds.MIR, sbp_bank_member_id=None,
    )
    assert "Заявка на выплату создана" in withdrawal_ui.edit.call_args.args[1]

    # A replayed tap must not create a second payout.
    replay = mock_callback("wd:confirm:" + nonce)
    await withdrawal_ui.confirm(replay, state)
    assert "уже отправлена" in replay.answer.call_args.args[0]
    assert len(payout_calls(client)) == 1

@pytest.mark.asyncio
async def test_sbp_flow_passes_bank_and_phone(setup):
    state, _, _ = setup
    client = make_client(providers=[provider(TransactionProviderIds.SBP, fee=2, methods=(), name="СБП")])
    use_client(client)
    await withdrawal_ui.start(mock_callback(), state)
    await withdrawal_ui.choose_provider(mock_callback("wd:provider:SBP"), state)
    assert "Выберите банк" in withdrawal_ui.edit.call_args.args[1]

    await withdrawal_ui.choose_bank(mock_callback("wd:bank:sber"), state)
    assert await state.get_state() == WithdrawalCreate.requisites.state

    message = mock_message("+7 (999) 123-45-67")
    await withdrawal_ui.input_requisites(message, state)
    message.delete.assert_awaited_once()
    data = await state.get_data()
    assert data["wd_requisites"] == "+79991234567"
    assert data["wd_masked"] == "+7 ••• ••• -6-7"
    assert "4111111111111111" not in str(data)

    await withdrawal_ui.input_amount(mock_message("500"), state)
    await withdrawal_ui.confirm(mock_callback("wd:confirm:" + (await state.get_data())["wd_nonce"]), state)
    client.call.assert_awaited_with(
        "request_withdrawal", provider=TransactionProviderIds.SBP, account="+79991234567",
        value=500, payment_method_id=None, sbp_bank_member_id="sber",
    )

@pytest.mark.asyncio
async def test_amount_and_requisites_are_validated(setup):
    state, _, _ = setup
    await withdrawal_ui.start(mock_callback(), state)
    await withdrawal_ui.choose_provider(mock_callback("wd:provider:BANK_CARD_RU"), state)
    await withdrawal_ui.choose_card(mock_callback(f"wd:card:{CARD_ID}"), state)
    over = mock_message("900")
    await withdrawal_ui.input_amount(over, state)
    assert "Недостаточно средств" in over.answer.call_args.args[0]
    assert await state.get_state() == WithdrawalCreate.amount.state
    zero = mock_message("0")
    await withdrawal_ui.input_amount(zero, state)
    assert "больше нуля" in zero.answer.call_args.args[0]

@pytest.mark.asyncio
async def test_card_must_be_linked_and_group_is_denied(setup):
    state, _, client = setup
    await withdrawal_ui.start(mock_callback(chat=ChatType.GROUP), state)
    assert await state.get_state() is None
    client.call.assert_not_awaited()

    use_client(make_client(cards=[]))
    await withdrawal_ui.start(mock_callback(), state)
    await withdrawal_ui.choose_provider(mock_callback("wd:provider:BANK_CARD_RU"), state)
    assert "нет привязанных карт" in withdrawal_ui.edit.call_args.args[1]
    assert await state.get_state() is None

@pytest.mark.asyncio
async def test_stale_or_blocked_account_is_refused(setup):
    state, account, _ = setup
    await withdrawal_ui.start(mock_callback(), state)
    withdrawal_ui.active_account.return_value = NS(
        id=uuid4(), tg_user_id=21, playerok_user_id="other", username="other",
    )
    await withdrawal_ui.choose_provider(mock_callback("wd:provider:BANK_CARD_RU"), state)
    assert "устарела" in withdrawal_ui.edit.call_args.args[1]
    assert await state.get_state() is None

    withdrawal_ui.active_account.return_value = account
    use_client(make_client(blocked=True))
    await withdrawal_ui.start(mock_callback(), state)
    await withdrawal_ui.choose_provider(mock_callback("wd:provider:BANK_CARD_RU"), state)
    await withdrawal_ui.choose_card(mock_callback(f"wd:card:{CARD_ID}"), state)
    assert "заблокирован" in withdrawal_ui.edit.call_args.args[1]

@pytest.mark.asyncio
async def test_payment_method_choice_is_offered_when_multiple(setup):
    state, _, _ = setup
    use_client(make_client(providers=[provider(methods=(
        TransactionPaymentMethodIds.MIR, TransactionPaymentMethodIds.VISA_MASTERCARD,
    ))]))
    await withdrawal_ui.start(mock_callback(), state)
    await withdrawal_ui.choose_provider(mock_callback("wd:provider:BANK_CARD_RU"), state)
    assert "платёжную систему" in withdrawal_ui.edit.call_args.args[1]
    await withdrawal_ui.choose_method(mock_callback("wd:method:VISA_MASTERCARD"), state)
    assert (await state.get_data())["wd_method"] == "VISA_MASTERCARD"
    assert "Выберите привязанную карту" in withdrawal_ui.edit.call_args.args[1]
