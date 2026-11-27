from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db import init_db, make_engine, make_session_factory
from app.miniapp import MiniApp
from app.models import PlayerokAccount, TelegramUser
from app.plugin_system import PluginManager
from playerokapi.enums import TransactionPaymentMethodIds, TransactionProviderIds
from tests.test_miniapp import TOKEN, authorize

CARD_ID = "0f3c8f5e-1f4b-4c47-9d5e-2b6f4a1c9d11"

def provider(identifier=TransactionProviderIds.BANK_CARD_RU, *, fee=6, methods=(TransactionPaymentMethodIds.MIR,)):
    return NS(
        id=identifier, name="Банковская карта", fee=fee, min_fee_amount=None, description=None,
        limits=NS(outgoing=NS(min=None, max=None)),
        payment_methods=[NS(id=method, name=method.name, fee=None) for method in methods],
    )

def pok_client(*, withdrawable="600.00"):
    cards = [NS(id=CARD_ID, card_first_six="411111", card_last_four="1111", card_type="MIR", is_chosen=True)]
    created = NS(
        id="tx-1", operation="WITHDRAW", status="PENDING", value="500.00", fee=30, created_at=None,
        props=NS(payment_account=NS(value="4111111111111111"), user_data=None),
    )

    async def call(method, *args, **kwargs):
        if method == "get_transaction_providers":
            return [provider()]
        if method == "get_verified_cards":
            return NS(bank_cards=cards)
        if method == "get_sbp_bank_members":
            return []
        if method == "get_transactions":
            return NS(transactions=[], page_info=NS(has_next_page=False))
        if method == "request_withdrawal":
            return created
        raise AssertionError(method)

    return NS(
        call=AsyncMock(side_effect=call),
        viewer=AsyncMock(return_value=NS(id="seller-1", is_blocked=False)),
        funds_protection=AsyncMock(return_value=False),
        balance=AsyncMock(return_value={"withdrawable": withdrawable, "available": withdrawable}),
    )

@pytest.mark.asyncio
async def test_withdrawal_endpoints_are_scoped_and_validated(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/payouts.db")
    await init_db(engine)
    db = make_session_factory(engine)
    try:
        async with db() as session:
            session.add_all([TelegramUser(id=101), TelegramUser(id=102)])
            await session.flush()
            own = PlayerokAccount(tg_user_id=101, playerok_user_id="seller-1", username="seller",
                                  cookies_encrypted="hidden", user_agent="test")
            foreign = PlayerokAccount(tg_user_id=102, playerok_user_id="seller-2", username="other",
                                      cookies_encrypted="hidden", user_agent="test")
            session.add_all([own, foreign])
            await session.commit()

        pok = pok_client()
        gateway = NS(get_client=AsyncMock(return_value=pok))
        app = MiniApp(db, gateway, PluginManager(db), TOKEN).create_app()
        async with TestClient(TestServer(app)) as client:
            headers = authorize()
            base = f"/api/accounts/{own.id}"

            unauthorized = await client.get(f"{base}/withdrawal-methods")
            assert unauthorized.status == 401

            options = await client.get(f"{base}/withdrawal-methods", headers=headers)
            assert options.status == 200
            data = await options.json()
            assert data["providers"][0]["id"] == "BANK_CARD_RU"
            assert data["providers"][0]["requisites"] == "card_id"
            assert data["cards"][0]["lastFour"] == "1111"
            assert data["withdrawable"] == "600.00"
            assert data["fundsProtection"] is False
            assert "4111111111111111" not in str(data)

            scoped = await client.get(f"/api/accounts/{foreign.id}/withdrawal-methods", headers=headers)
            assert scoped.status == 404

            unconfirmed = await client.post(f"{base}/withdrawals", headers=headers, json={
                "provider": "BANK_CARD_RU", "account": CARD_ID, "value": 500,
            })
            assert unconfirmed.status == 400
            assert "Подтвердите" in (await unconfirmed.json())["error"]

            bad_amount = await client.post(f"{base}/withdrawals", headers=headers, json={
                "provider": "BANK_CARD_RU", "account": CARD_ID, "value": 500.5, "confirm": True,
            })
            assert bad_amount.status == 400

            too_much = await client.post(f"{base}/withdrawals", headers=headers, json={
                "provider": "BANK_CARD_RU", "account": CARD_ID, "value": 900, "confirm": True,
            })
            assert too_much.status == 400
            assert "Недостаточно средств" in (await too_much.json())["error"]

            created = await client.post(f"{base}/withdrawals", headers=headers, json={
                "provider": "BANK_CARD_RU", "account": CARD_ID, "value": 500,
                "paymentMethodId": "MIR", "confirm": True,
            })
            assert created.status == 201
            payload = await created.json()
            assert payload["masked"] == "•••• 1111"
            assert payload["requested"] == "500"
            assert payload["remaining"] == "100.00"
            assert "4111111111111111" not in str(payload)
            pok.call.assert_awaited_with(
                "request_withdrawal", provider=TransactionProviderIds.BANK_CARD_RU, account=CARD_ID,
                value=500, payment_method_id=TransactionPaymentMethodIds.MIR, sbp_bank_member_id=None,
            )

            repeated = await client.post(f"{base}/withdrawals", headers=headers, json={
                "provider": "BANK_CARD_RU", "account": CARD_ID, "value": 500, "confirm": True,
            })
            assert repeated.status == 409
    finally:
        await engine.dispose()
