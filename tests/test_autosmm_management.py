from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.autosmm_manager import (
    AutoSmmConfig, AutoSmmLotRule, AutoSmmOrder, SmmApiClient, SmmError,
    bind_service, normalize_api_url, refresh_order, request_order_refill, resolve_lot_rule,
    service_refill_allowed,
)
from app.db import init_db, make_engine, make_session_factory
from app.miniapp import MiniApp
from app.models import PlayerokAccount, TelegramUser
from app.plugin_system import PluginManager
from tests.test_miniapp import TOKEN, authorize


SERVICE = {"service": "42", "name": "Услуга A", "category": "Раздел", "min": "10", "max": "500", "rate": "1.5", "refill": True}
OTHER = {**SERVICE, "service": "43", "name": "Услуга B", "refill": False}
URL = "https://example.com/api/v2"


@pytest_asyncio.fixture
async def setup(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/smm.db")
    await init_db(engine)
    db = make_session_factory(engine)
    try:
        async with db() as session:
            session.add_all([TelegramUser(id=101), TelegramUser(id=102)])
            await session.flush()
            own = PlayerokAccount(tg_user_id=101, username="own", playerok_user_id="seller",
                                  cookies_encrypted="hidden", user_agent="test")
            foreign = PlayerokAccount(tg_user_id=102, username="foreign", playerok_user_id="foreign",
                                      cookies_encrypted="hidden", user_agent="test")
            session.add_all([own, foreign])
            await session.flush()
            session.add_all([
                AutoSmmConfig(account_id=own.id, api_url=URL, api_key_encrypted="secret"),
                AutoSmmOrder(account_id=own.id, deal_id="deal", chat_id="chat", smm_order_id="44",
                             service_id="42", service_name="Услуга A", quantity=100, link="https://example.org/item",
                             status="Pending", provider_url=URL),
                AutoSmmOrder(account_id=foreign.id, deal_id="foreign", chat_id="chat2", smm_order_id="45",
                             service_id="42", quantity=100, link="https://example.org/other", status="Pending", provider_url=URL),
            ])
            await session.commit()
            order = await session.scalar(select(AutoSmmOrder).where(AutoSmmOrder.account_id == own.id))
            чужой = await session.scalar(select(AutoSmmOrder).where(AutoSmmOrder.account_id == foreign.id))
        yield db, own, foreign, order.id, чужой.id
    finally:
        await engine.dispose()


def panel():
    return NS(api_url=URL, get_balance=AsyncMock(return_value={"balance": "15.50", "currency": "RUB"}),
              get_services=AsyncMock(return_value=[SERVICE, OTHER]),
              get_order_status=AsyncMock(return_value={"status": "Completed"}),
              request_refill=AsyncMock(return_value={"refill": 123}),
              get_refill_status=AsyncMock(return_value={"status": "In progress"}))


@pytest.mark.asyncio
async def test_keyword_binding_priorities_and_limits(setup):
    db, own, foreign, *_ = setup
    broad = await bind_service(db, own.id, service=SERVICE, quantity=100, keyword="  Премиум ")
    repeated = await bind_service(db, own.id, service=SERVICE, quantity=101, keyword="премиум")
    assert repeated.id == broad.id
    specific = await bind_service(db, own.id, service=OTHER, quantity=200, keyword="Премиум набор")
    exact = await bind_service(db, own.id, service=SERVICE, quantity=30, lot_id="lot", lot_title="Премиум набор")
    await bind_service(db, foreign.id, service=SERVICE, quantity=30, keyword="премиум набор")
    async with db() as session:
        assert (await resolve_lot_rule(session, own.id, "new-lot", "ПРЕМИУМ   набор!")).id == specific.id
        assert (await resolve_lot_rule(session, own.id, "lot", "Премиум набор")).id == exact.id
        assert await resolve_lot_rule(session, own.id, "new-lot", "суперпремиум") is None
        row = await session.get(AutoSmmLotRule, exact.id)
        row.enabled = False
        await session.commit()
    async with db() as session:
        assert await resolve_lot_rule(session, own.id, "lot", "Премиум набор") is None
    with pytest.raises(SmmError):
        await bind_service(db, own.id, service=SERVICE, quantity=501, keyword="test")
    with pytest.raises(SmmError):
        await bind_service(db, own.id, service=SERVICE, quantity=100, keyword="*")


@pytest.mark.asyncio
async def test_refill_happy_path_and_no_duplicate(setup):
    db, own, foreign, order_id, foreign_id = setup
    client = panel()
    order, can_refill = await refresh_order(db, own.id, order_id, client)
    assert can_refill and order.status == "Completed"
    with pytest.raises(SmmError):
        await request_order_refill(db, own.id, foreign_id, client)
    result = await request_order_refill(db, own.id, order_id, client)
    assert result.refill_id == "123" and result.refill_status == "Pending"
    client.request_refill.assert_awaited_once_with("44")
    with pytest.raises(SmmError):
        await request_order_refill(db, own.id, order_id, client)
    result, can_refill = await refresh_order(db, own.id, order_id, client)
    assert result.refill_status == "In progress" and not can_refill
    client.get_refill_status.assert_awaited_once_with("123")
    client.request_refill.assert_awaited_once_with("44")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["unsupported", "not_completed", "wrong_panel", "legacy", "timeout"])
async def test_refill_safely_rejects_or_reserves_uncertain_requests(setup, reason):
    db, own, _, order_id, _ = setup
    client = panel()
    if reason == "unsupported":
        client.get_services.return_value = [{**SERVICE, "refill": "false"}]
    if reason == "not_completed":
        client.get_order_status.return_value = {"status": "Processing"}
    if reason == "wrong_panel":
        client.api_url = "https://other.example.org/api/v2"
    if reason == "legacy":
        async with db() as session:
            order = await session.get(AutoSmmOrder, order_id)
            order.provider_url = None
            await session.commit()
    if reason == "timeout":
        client.request_refill.side_effect = TimeoutError()
    with pytest.raises(SmmError):
        await request_order_refill(db, own.id, order_id, client)
    if reason == "timeout":
        async with db() as session:
            assert (await session.get(AutoSmmOrder, order_id)).refill_status == "Unknown"
        with pytest.raises(SmmError):
            await request_order_refill(db, own.id, order_id, client)
        client.request_refill.assert_awaited_once()
    else:
        client.request_refill.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_legacy_confirmation(setup):
    db, own, _, order_id, _ = setup
    async with db() as session:
        order = await session.get(AutoSmmOrder, order_id)
        order.provider_url = None
        await session.commit()
    result = await request_order_refill(db, own.id, order_id, panel(), confirm_legacy=True)
    assert result.provider_url == URL


@pytest.mark.asyncio
async def test_miniapp_autosmm_routes_account_scope_confirmation_and_catalog(setup, monkeypatch):
    db, own, foreign, order_id, foreign_id = setup
    client = panel()
    import app.miniapp_autosmm as mini
    monkeypatch.setattr(mini, "get_configured_client", AsyncMock(return_value=(client, None)))
    gateway = NS(cipher=object(), get_client=AsyncMock())
    app = MiniApp(db, gateway, PluginManager(db), TOKEN).create_app()
    async with TestClient(TestServer(app)) as http:
        base = f"/api/accounts/{own.id}/autosmm"
        headers = authorize()
        response = await http.get(base, headers=headers)
        assert response.status == 200
        data = await response.json()
        assert data["configured"] and "secret" not in str(data)
        response = await http.get(f"/api/accounts/{foreign.id}/autosmm", headers=headers)
        assert response.status == 404
        balance = await (await http.get(base + "/balance", headers=headers)).json()
        assert balance == {"balance": "15.50", "currency": "RUB"}
        catalog = await (await http.get(base + "/catalog?q=42", headers=headers)).json()
        assert catalog["total"] == 1 and catalog["items"][0]["refill"]
        response = await http.post(base + "/rules", headers=headers, json={"keyword": "Премиум", "serviceId": "42", "quantity": 100})
        assert response.status == 201, await response.text()
        rule = await response.json()
        assert rule["keyword"] == "Премиум"
        response = await http.patch(base + f"/rules/{rule['id']}", headers=headers, json={"quantity": 150})
        assert (await response.json())["quantity"] == 150
        orders = await (await http.get(base + "/orders", headers=headers)).json()
        assert orders["total"] == 1 and orders["items"][0]["id"] == order_id
        response = await http.get(base + f"/orders/{foreign_id}", headers=headers)
        assert response.status == 400
        detail = await (await http.get(base + f"/orders/{order_id}", headers=headers)).json()
        assert detail["canRefill"]
        refill_url = base + f"/orders/{order_id}/refill"
        assert (await http.post(refill_url, headers=headers, json={})).status == 400
        client.request_refill.assert_not_awaited()
        response = await http.post(refill_url, headers=headers, json={"confirm": True})
        assert response.status == 200, await response.text()
        assert (await response.json())["refillId"] == "123"
        assert (await http.post(refill_url, headers=headers, json={"confirm": True})).status == 400
        client.request_refill.assert_awaited_once_with("44")


def test_refill_truthiness_and_public_url_validation():
    for value in [True, 1, "1", "true", "yes"]:
        assert service_refill_allowed({"refill": value})
    for value in [False, 0, "0", "false", "no", None]:
        assert not service_refill_allowed({"refill": value})
    assert normalize_api_url(URL) == URL
    for url in ["http://example.com", "https://localhost", "https://127.0.0.1", "https://10.0.0.1", "https://a.internal", "https://example.com?key=secret"]:
        with pytest.raises(SmmError):
            normalize_api_url(url)


@pytest.mark.asyncio
async def test_provider_api_refill_actions():
    client = SmmApiClient(URL, "test")
    client._post = AsyncMock(side_effect=[{"refill": 10}, {"status": "Completed"}])
    assert await client.request_refill("44") == {"refill": 10}
    assert await client.get_refill_status("10") == {"status": "Completed"}
    assert [call.args[0] for call in client._post.await_args_list] == [
        {"action": "refill", "order": "44"}, {"action": "refill_status", "refill": "10"},
    ]


@pytest.mark.asyncio
async def test_concurrent_refill_is_reserved_before_provider_request(setup):
    db, own, _, order_id, _ = setup
    client = panel()
    entered, release = asyncio.Event(), asyncio.Event()

    async def refill(_):
        entered.set()
        await release.wait()
        return {"refill": 200}

    client.request_refill.side_effect = refill
    first = asyncio.create_task(request_order_refill(db, own.id, order_id, client))
    await asyncio.wait_for(entered.wait(), timeout=3)
    try:
        with pytest.raises(SmmError):
            await request_order_refill(db, own.id, order_id, client)
    finally:
        release.set()
        await first
    client.request_refill.assert_awaited_once_with("44")


@pytest.mark.asyncio
async def test_old_database_columns_migrate_without_losing_records(tmp_path):
    from sqlalchemy import text
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/old.db")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE autosmm_lot_rules (id INTEGER PRIMARY KEY, lot_title VARCHAR(256))"))
            await conn.execute(text("INSERT INTO autosmm_lot_rules VALUES (1, 'old')"))
            await conn.execute(text("CREATE TABLE autosmm_orders (id INTEGER PRIMARY KEY, status VARCHAR(64))"))
            await conn.execute(text("INSERT INTO autosmm_orders VALUES (1, 'Pending')"))
        await init_db(engine)
        await init_db(engine)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT lot_title, keyword FROM autosmm_lot_rules"))).one() == ("old", None)
            assert (await conn.execute(text("SELECT status, provider_url, refill_id, refill_status FROM autosmm_orders"))).one() == ("Pending", None, None, None)
    finally:
        await engine.dispose()
