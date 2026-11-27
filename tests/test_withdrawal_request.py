from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from playerokapi.enums import (
    TransactionPaymentMethodIds, TransactionProviderDirections, TransactionProviderIds,
)
from app.withdrawal_request import (
    WithdrawalError, create_withdrawal, fee_preview, mask_requisites, parse_amount,
    parse_card_id, parse_card_number, parse_phone, parse_requisites, parse_wallet, provider_json,
    requisite_kind, withdrawal_methods,
)

CARD_ID = "0f3c8f5e-1f4b-4c47-9d5e-2b6f4a1c9d11"
CARD_ID_2 = "7a1d2c3b-4e5f-4a6b-8c7d-9e0f1a2b3c4d"

def provider(identifier=TransactionProviderIds.BANK_CARD_RU, *, fee=6, min_fee=None,
             minimum=None, maximum=None, methods=(), name="Банковская карта"):
    return NS(
        id=identifier, name=name, fee=fee, min_fee_amount=min_fee, description=None,
        limits=NS(outgoing=NS(min=minimum, max=maximum)),
        payment_methods=[NS(id=method, name=method.name, fee=None) for method in methods],
    )

def card(identifier=CARD_ID, last_four="1111", chosen=True):
    return NS(id=identifier, card_first_six="411111", card_last_four=last_four,
              card_type="MIR", is_chosen=chosen)

def client(*, providers=None, cards=None, banks=None, withdrawable="600.00", blocked=False,
           viewer_id="seller", transaction=None):
    providers = [provider(methods=(TransactionPaymentMethodIds.MIR,))] if providers is None else providers
    cards = [card()] if cards is None else cards
    banks = [NS(id="sber", name="Сбербанк")] if banks is None else banks
    result = NS(
        id="tx-1", operation="WITHDRAW", status="PROCESSING", value="500.00", fee=30,
        created_at=None, props=NS(payment_account=NS(value="4111111111111111"), user_data=None),
    ) if transaction is None else transaction

    async def call(method, *args, **kwargs):
        if method == "get_transaction_providers":
            assert args and args[0] is TransactionProviderDirections.OUT
            return providers
        if method == "get_verified_cards":
            return NS(bank_cards=cards)
        if method == "get_sbp_bank_members":
            return banks
        if method == "request_withdrawal":
            return result
        raise AssertionError(method)

    return NS(
        call=AsyncMock(side_effect=call),
        viewer=AsyncMock(return_value=NS(id=viewer_id, is_blocked=blocked)),
        balance=AsyncMock(return_value={"withdrawable": withdrawable}),
    )

@pytest.mark.parametrize("raw,expected", [("500", 500), (" 1 200 ", 1200), (500, 500)])
def test_amount_accepts_whole_rubles(raw, expected):
    assert parse_amount(raw) == expected

@pytest.mark.parametrize("raw", ["0", "-5", "10.50", "1e3", "abc", "", None, True, "9" * 11])
def test_amount_rejects_everything_else(raw):
    with pytest.raises(WithdrawalError):
        parse_amount(raw)

def test_card_number_requires_luhn():
    assert parse_card_number("4111 1111 1111 1111") == "4111111111111111"
    for raw in ("4111111111111112", "411111", "41111111111111111111", "", None):
        with pytest.raises(WithdrawalError):
            parse_card_number(raw)

@pytest.mark.parametrize("raw,expected", [
    ("+7 (999) 123-45-67", "+79991234567"),
    ("89991234567", "+79991234567"),
    ("9991234567", "+79991234567"),
])
def test_phone_is_normalized(raw, expected):
    assert parse_phone(raw) == expected

@pytest.mark.parametrize("raw", ["123", "+1 555 0100", "7999123456", "", None])
def test_phone_rejects_non_ru_numbers(raw):
    with pytest.raises(WithdrawalError):
        parse_phone(raw)

def test_wallet_and_card_id_formats():
    wallet = "T" + "a" * 33
    assert parse_wallet(wallet) == wallet
    with pytest.raises(WithdrawalError):
        parse_wallet("0x" + "a" * 40)
    assert parse_card_id(CARD_ID.upper()) == CARD_ID
    with pytest.raises(WithdrawalError):
        parse_card_id("4111111111111111")

def test_requisites_dispatch_by_provider():
    assert requisite_kind(TransactionProviderIds.BANK_CARD_RU) == "card_id"
    assert requisite_kind(TransactionProviderIds.SBP) == "phone"
    assert requisite_kind(TransactionProviderIds.USDT) == "wallet"
    assert requisite_kind(TransactionProviderIds.BANK_CARD) == "card_number"
    assert requisite_kind(TransactionProviderIds.BANK_CARD_BY) == "account"
    assert parse_requisites("card_id", CARD_ID) == CARD_ID
    assert parse_requisites("account", "BY12 ABCD 1234") == "BY12 ABCD 1234"

@pytest.mark.parametrize("kind,value,expected", [
    ("card_id", CARD_ID, "•••• 9d11"),
    ("card_number", "4111111111111111", "•••• 1111"),
    ("phone", "+79991234567", "+7 ••• ••• -6-7"),
    ("wallet", "T" + "a" * 30 + "wxyz", "•••• wxyz"),
])
def test_mask_never_returns_full_requisites(kind, value, expected):
    masked = mask_requisites(kind, value)
    assert masked == expected
    assert value not in masked

def test_fee_preview_uses_percent_and_floor():
    assert fee_preview(provider(fee=6), 500) == {"fee": Decimal("30.00"), "payout": Decimal("470.00")}
    floored = fee_preview(provider(fee=1, min_fee=50), 100)
    assert floored["fee"] == Decimal("50.00")
    assert floored["payout"] == Decimal("50.00")

def test_provider_json_skips_unknown_and_keeps_limits():
    assert provider_json(NS(id="SOMETHING_NEW", name="x")) is None
    payload = provider_json(provider(minimum=100, maximum=5000, methods=(TransactionPaymentMethodIds.MIR,)))
    assert payload["id"] == "BANK_CARD_RU"
    assert payload["requisites"] == "card_id"
    assert payload["minValue"] == "100"
    assert payload["maxValue"] == "5000"
    assert isinstance(payload["fee"], str)
    assert payload["paymentMethods"][0]["id"] == "MIR"
    assert payload["needsBank"] is False

@pytest.mark.asyncio
async def test_methods_load_cards_and_banks_only_when_needed():
    poker = client(providers=[provider(TransactionProviderIds.SBP, methods=())])
    result = await withdrawal_methods(poker)
    assert result["providers"][0]["needsBank"] is True
    assert result["banks"] == [{"id": "sber", "name": "Сбербанк"}]
    assert result["cards"] == []
    assert result["walletUrl"].startswith("https://playerok.com/")
    poker.call.assert_any_await("get_sbp_bank_members")

@pytest.mark.asyncio
async def test_create_payout_sends_validated_mutation():
    poker = client()
    result = await create_withdrawal(
        poker, "seller", provider_id="BANK_CARD_RU", account=CARD_ID, value="500",
        payment_method_id="MIR",
    )
    poker.call.assert_awaited_with(
        "request_withdrawal",
        provider=TransactionProviderIds.BANK_CARD_RU,
        account=CARD_ID,
        value=500,
        payment_method_id=TransactionPaymentMethodIds.MIR,
        sbp_bank_member_id=None,
    )
    assert result["masked"] == "•••• 1111"
    assert result["requested"] == "500"
    assert result["remaining"] == "100.00"
    assert result["statusLabel"] == "В обработке"
    assert "4111111111111111" not in str(result)

@pytest.mark.asyncio
async def test_sbp_payout_requires_listed_bank():
    poker = client(providers=[provider(TransactionProviderIds.SBP, fee=2, methods=())])
    result = await create_withdrawal(
        poker, "seller", provider_id="SBP", account="+7 999 123-45-67", value=500,
        sbp_bank_member_id="sber",
    )
    poker.call.assert_awaited_with(
        "request_withdrawal", provider=TransactionProviderIds.SBP,
        account="+79991234567", value=500, payment_method_id=None, sbp_bank_member_id="sber",
    )
    assert result["masked"] == "+7 ••• ••• -6-7"
    with pytest.raises(WithdrawalError):
        await create_withdrawal(poker, "seller", provider_id="SBP", account="+79991234567",
                                value=500, sbp_bank_member_id="unknown-bank")

@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"provider_id": "LOCAL", "account": "1", "value": 100},
    {"provider_id": "BANK_CARD_RU", "account": CARD_ID_2, "value": 100},
    {"provider_id": "BANK_CARD_RU", "account": "not-a-card", "value": 100},
    {"provider_id": "BANK_CARD_RU", "account": CARD_ID, "value": 700},
    {"provider_id": "BANK_CARD_RU", "account": CARD_ID, "value": 0},
    {"provider_id": "BANK_CARD_RU", "account": CARD_ID, "value": 100, "payment_method_id": "VISA_MASTERCARD"},
    {"provider_id": "BANK_CARD_RU", "account": CARD_ID, "value": 100, "sbp_bank_member_id": "sber"},
])
async def test_create_payout_rejects_unsafe_input(kwargs):
    poker = client()
    with pytest.raises(WithdrawalError):
        await create_withdrawal(poker, "seller", **kwargs)
    assert not any(
        call.args and call.args[0] == "request_withdrawal" for call in poker.call.await_args_list
    )

@pytest.mark.asyncio
async def test_payment_method_is_auto_selected_only_when_unique():
    single = client()
    await create_withdrawal(single, "seller", provider_id="BANK_CARD_RU", account=CARD_ID, value=100)
    single.call.assert_awaited_with(
        "request_withdrawal", provider=TransactionProviderIds.BANK_CARD_RU, account=CARD_ID,
        value=100, payment_method_id=TransactionPaymentMethodIds.MIR, sbp_bank_member_id=None,
    )
    multiple = client(providers=[provider(methods=(
        TransactionPaymentMethodIds.MIR, TransactionPaymentMethodIds.VISA_MASTERCARD,
    ))])
    with pytest.raises(WithdrawalError):
        await create_withdrawal(multiple, "seller", provider_id="BANK_CARD_RU", account=CARD_ID, value=100)
    await create_withdrawal(
        multiple, "seller", provider_id="BANK_CARD_RU", account=CARD_ID, value=100,
        payment_method_id="VISA_MASTERCARD",
    )
    multiple.call.assert_awaited_with(
        "request_withdrawal", provider=TransactionProviderIds.BANK_CARD_RU, account=CARD_ID,
        value=100, payment_method_id=TransactionPaymentMethodIds.VISA_MASTERCARD, sbp_bank_member_id=None,
    )

@pytest.mark.asyncio
async def test_create_payout_enforces_provider_limits():
    poker = client(providers=[provider(minimum=200, maximum=400, methods=(TransactionPaymentMethodIds.MIR,))])
    with pytest.raises(WithdrawalError):
        await create_withdrawal(poker, "seller", provider_id="BANK_CARD_RU", account=CARD_ID, value=100)
    with pytest.raises(WithdrawalError):
        await create_withdrawal(poker, "seller", provider_id="BANK_CARD_RU", account=CARD_ID, value=500)

@pytest.mark.asyncio
async def test_create_payout_requires_offered_provider_and_account():
    poker = client(providers=[provider(TransactionProviderIds.SBP, methods=())])
    with pytest.raises(WithdrawalError):
        await create_withdrawal(poker, "seller", provider_id="BANK_CARD_RU", account=CARD_ID, value=100)
    with pytest.raises(WithdrawalError):
        await create_withdrawal(client(blocked=True), "seller", provider_id="BANK_CARD_RU",
                                account=CARD_ID, value=100)
    with pytest.raises(WithdrawalError):
        await create_withdrawal(client(viewer_id="other"), "seller", provider_id="BANK_CARD_RU",
                                account=CARD_ID, value=100)
