from __future__ import annotations

import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from app.db import init_db, make_engine, make_session_factory
from app.miniapp import MiniApp
from app.models import PlayerokAccount, TelegramUser
from app.plugin_system import PluginManager
from playerokapi.enums import GameCategoryDataFieldTypes
from tests.test_miniapp import TOKEN, authorize


def page(**kwargs):
    return NS(page_info=NS(has_next_page=False, end_cursor=None), total_count=1, **kwargs)


def field(id, label, *, required=False, hidden=False):
    return NS(id=id, label=label, type=GameCategoryDataFieldTypes.ITEM_DATA, required=required, hidden=hidden)


def item(status="DRAFT", owner="seller", price=125):
    return NS(id="lot", name="Подарочный набор", price=price, raw_price=price,
              status=status, description="Описание", slug="lot", user=NS(id=owner),
              attachments=[NS(id="photo", url="https://cdn.playerok.com/lot.webp")])


def fake_pok():
    game = NS(id="game", name="Игра", logo=NS(url="https://cdn.playerok.com/game.webp"))
    groups = [
        NS(field="color", group="Цвет", label="Зелёный", value="green", type="SELECTOR", multiple=False),
        NS(field="count", group="Количество", type="RANGE", value_range_limit=NS(min=1, max=5)),
        NS(field="tags", group="Метки", label="Подарок", value="gift", type="SELECTOR", multiple=True),
    ]
    category = NS(id="category", name="Предметы", options=groups)
    game.categories = [category]
    fields = [field("buyer_message", "Сообщение покупателю"), field("size", "Размер", required=True),
              field("private", "Пароль", hidden=True)]
    current = item()
    client = AsyncMock()
    client.get_item.side_effect = lambda id: current if id == "lot" else item(owner="foreign")
    client.create_item.return_value = current
    client.get_item_priority_statuses.return_value = [NS(id="free", name="Стандарт", price=0),
                                                     NS(id="premium", name="Премиум", price=25)]
    client.balance.return_value = {"available": "100"}
    client.publish_item.return_value = item(status="PENDING_APPROVAL")

    async def call(operation, *args, **kwargs):
        if operation == "get_games":
            return page(games=[game])
        if operation == "get_game":
            return game
        if operation == "get_game_category":
            return category
        if operation == "get_game_category_obtaining_types":
            return page(obtaining_types=[NS(id="manual", name="Вручную")])
        if operation == "get_game_category_data_fields":
            return page(data_fields=fields)
        raise AssertionError(operation)

    client.call.side_effect = call
    return client, current, fields


def draft_body(**changes):
    return {
        "categoryId": "category", "methodId": "manual", "name": "Подарочный набор",
        "description": "Описание", "price": 125, "comment": "Спасибо за покупку!",
        "options": {"color": "green", "count": 3, "tags": ["gift"]}, "fields": {"size": "M"},
    } | changes


@pytest_asyncio.fixture
async def portal(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/listings.db")
    await init_db(engine)
    db = make_session_factory(engine)
    try:
        async with db() as session:
            session.add_all([TelegramUser(id=101), TelegramUser(id=102)])
            await session.flush()
            own = PlayerokAccount(tg_user_id=101, playerok_user_id="seller", username="seller",
                                  cookies_encrypted="hidden", user_agent="test")
            foreign = PlayerokAccount(tg_user_id=102, playerok_user_id="foreign", username="foreign",
                                      cookies_encrypted="hidden", user_agent="test")
            session.add_all([own, foreign])
            await session.commit()
        pok, current, fields = fake_pok()
        gateway = NS(get_client=AsyncMock(return_value=pok))
        miniapp = MiniApp(db, gateway, PluginManager(db), TOKEN)
        async with TestClient(TestServer(miniapp.create_app())) as client:
            yield client, f"/api/accounts/{own.id}", f"/api/accounts/{foreign.id}", pok, current, fields
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_and_multipart_draft_creation(portal):
    client, base, foreign, pok, _, _ = portal
    headers = authorize()
    for route in [f"{foreign}/catalog/games", f"{foreign}/listings/lot/publication"]:
        assert (await client.get(route, headers=headers)).status == 404
    games = await client.get(f"{base}/catalog/games?q=Игра", headers=headers)
    assert (await games.json())["items"][0]["name"] == "Игра"
    pok.call.assert_awaited_with("get_games", name="Игра", count=20, after_cursor=None)
    game = await client.get(f"{base}/catalog/games/game", headers=headers)
    assert (await game.json())["categories"] == [{"id": "category", "name": "Предметы"}]
    category = await client.get(f"{base}/catalog/categories/category", headers=headers)
    assert not (await category.json())["ready"]
    schema_res = await client.get(f"{base}/catalog/categories/category?method=manual", headers=headers)
    schema = await schema_res.json()
    assert schema["ready"] and not schema["blocked"]
    assert schema["fields"] == [{"id": "size", "label": "Размер", "required": True}]
    assert schema["options"][1]["maximum"] == 5

    image = b"\x89PNG\r\n\x1a\n" + b"x" * 100_000
    body = FormData()
    body.add_field("data", json.dumps(draft_body(), ensure_ascii=False))
    body.add_field("image", image, filename="photo.png", content_type="image/png")
    created = await client.post(f"{base}/listings", headers=headers, data=body)
    assert created.status == 201, await created.text()
    result = await created.json()
    assert result["status"] == "DRAFT" and result["canPublish"]
    pok.create_item.assert_awaited_once_with(
        game_category_id="category", obtaining_type_id="manual", name="Подарочный набор",
        description="Описание", price=125, options={"color": "green", "count": 3, "tags": ["gift"]},
        data_fields={"size": "M", "buyer_message": "Спасибо за покупку!"}, comment=None, attachments=[image],
    )
    pok.publish_item.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"price": True}, {"price": 0}, {"methodId": "wrong"}, {"methodId": None},
    {"options": {"color": "fake"}}, {"options": {"count": 6}}, {"fields": {}},
    {"fields": {"size": "M", "private": "forbidden"}}, {"comment": "-"},
])
async def test_draft_validates_catalog_and_required_fields(portal, changes):
    client, base, _, pok, _, _ = portal
    response = await client.post(f"{base}/listings", headers=authorize(), json=draft_body(**changes))
    assert response.status == 400
    pok.create_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_private_fields_are_not_requested(portal):
    client, base, _, pok, _, fields = portal
    fields[-1].required = True
    response = await client.get(f"{base}/catalog/categories/category?method=manual", headers=authorize())
    assert (await response.json())["blocked"]
    response = await client.post(f"{base}/listings", headers=authorize(), json=draft_body())
    assert response.status == 400
    pok.create_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_requires_owned_item_and_single_use_quote(portal):
    client, base, _, pok, _, _ = portal
    headers = authorize()
    assert (await client.get(f"{base}/listings/foreign/publication", headers=headers)).status == 404
    endpoint = f"{base}/listings/lot/publication"
    response = await client.get(endpoint, headers=headers)
    assert response.status == 200
    quote = await response.json()
    assert quote["tariffs"] == [
        {"id": "free", "name": "Стандарт", "fee": "0", "available": True, "remaining": "100"},
        {"id": "premium", "name": "Премиум", "fee": "25", "available": True, "remaining": "75"},
    ]
    for payload in [{"token": quote["token"], "tariffId": "premium"}, {"token": "bad", "tariffId": "premium", "confirm": True}]:
        assert (await client.post(endpoint, headers=headers, json=payload)).status in {400, 409}
    pok.publish_item.assert_not_awaited()
    payload = {"token": quote["token"], "tariffId": "premium", "confirm": True}
    response = await client.post(endpoint, headers=headers, json=payload)
    assert response.status == 200, await response.text()
    assert (await response.json())["status"] == "PENDING_APPROVAL"
    assert (await client.post(endpoint, headers=headers, json=payload)).status == 409
    pok.publish_item.assert_awaited_once_with("lot", "premium")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["price", "balance", "tariff"])
async def test_publication_rechecks_price_balance_and_tariff(portal, change):
    client, base, _, pok, item_record, _ = portal
    endpoint = f"{base}/listings/lot/publication"
    headers = authorize()
    quote = await (await client.get(endpoint, headers=headers)).json()
    if change == "price":
        item_record.price = 200
    elif change == "balance":
        pok.balance.return_value = {"available": "90"}
    else:
        pok.get_item_priority_statuses.return_value = [NS(id="premium", name="Премиум", price=35)]
    response = await client.post(endpoint, headers=headers, json={"token": quote["token"], "tariffId": "premium", "confirm": True})
    assert response.status == 409
    pok.publish_item.assert_not_awaited()

@pytest.mark.asyncio
async def test_free_publication_remains_available_without_balance(portal):
    client, base, _, pok, _, _ = portal
    pok.balance.return_value = {}
    pok.viewer.return_value = NS(available_balance=None)
    endpoint = f"{base}/listings/lot/publication"
    headers = authorize()
    quote = await (await client.get(endpoint, headers=headers)).json()
    assert quote["balance"] is None
    assert quote["tariffs"][0]["available"]
    assert not quote["tariffs"][1]["available"]
    response = await client.post(endpoint, headers=headers, json={"token": quote["token"], "tariffId": "premium", "confirm": True})
    assert response.status == 400
    pok.publish_item.assert_not_awaited()
    response = await client.post(endpoint, headers=headers, json={"token": quote["token"], "tariffId": "free", "confirm": True})
    assert response.status == 200
    pok.publish_item.assert_awaited_once_with("lot", "free")


@pytest.mark.asyncio
async def test_draft_rejects_non_image_upload(portal):
    client, base, _, pok, _, _ = portal
    body = FormData()
    body.add_field("data", json.dumps(draft_body(), ensure_ascii=False))
    body.add_field("image", b"not a photo", filename="photo.png", content_type="image/png")
    response = await client.post(f"{base}/listings", headers=authorize(), data=body)
    assert response.status == 400
    pok.create_item.assert_not_awaited()
