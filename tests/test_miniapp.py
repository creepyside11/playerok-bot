from __future__ import annotations

import hmac
import json
import time
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from app.db import init_db, make_engine, make_session_factory
from app.miniapp import MAX_AUTH_AGE, MiniApp, validate_telegram_user
from app.models import PlayerokAccount, TelegramUser
from app.plugin_system import PluginManager, PluginSpec
from app.telegram_accounts_manager import TelegramAccountStock
from playerokapi.enums import TransactionOperations


TOKEN = "12345:local_test_token"


def init_data(user: int, timestamp: int | None = None) -> str:
    fields = {
        "auth_date": str(int(time.time()) if timestamp is None else timestamp),
        "user": json.dumps({"id": user, "first_name": "Продавец"}, ensure_ascii=False),
        "signature": "example_signature",
    }
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    payload = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.digest(secret, payload.encode(), "sha256").hex()
    return urlencode(fields)


def authorize(user: int = 101) -> dict[str, str]:
    return {"Authorization": "tma " + init_data(user)}


def test_telegram_signature_timestamp_and_duplicate_check():
    sample = init_data(101, 1700000000)
    identity = validate_telegram_user(sample, TOKEN, now=1700000001)
    assert identity["id"] == 101
    for raw, now in (
        (sample.replace("101", "102"), 1700000001),
        (sample, 1700000000 + MAX_AUTH_AGE + 1),
        (sample, 1699999939),
        (sample + "&auth_date=1700000000", 1700000001),
    ):
        with pytest.raises(ValueError):
            validate_telegram_user(raw, TOKEN, now=now)


@pytest.mark.asyncio
async def test_portal_uses_owned_accounts_shared_plugin_state_and_chats(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/portal.db")
    await init_db(engine)
    db = make_session_factory(engine)
    try:
        async with db() as session:
            session.add_all([TelegramUser(id=101), TelegramUser(id=102)])
            await session.flush()
            accounts = [
                PlayerokAccount(tg_user_id=uid, playerok_user_id=f"player-{uid}", username=f"seller-{uid}",
                                cookies_encrypted="hidden", user_agent="test")
                for uid in (101, 102)
            ]
            session.add_all(accounts)
            await session.commit()
            own, foreign = accounts

        plugins = PluginManager(db)
        plugin = PluginSpec(
            "example", "Плагин", "1.0", "Тест", "Example",
            {"enabled_feature": {"label": "Функция", "type": "bool", "default": True},
             "golden_key": {"label": "Ключ", "type": "str", "default": "must-not-leave-server"}},
            ModuleType("example"),
        )
        plugins.plugins[plugin.id] = plugin

        buyer = NS(id="buyer", username="buyer", avatar_url=None)
        seller = NS(id=own.playerok_user_id, username=own.username)
        incoming = NS(id="msg-1", text="Купил товар", user=buyer, images=[], file=None, created_at=None)
        reply = NS(id="msg-2", text="Ответ", user=seller, images=[], file=None, created_at=None)
        payment = NS(id="msg-3", text="{ITEM_PAID}", user=None, images=[], created_at=None,
                     deal=NS(id="deal", item=NS(id="lot", name="Набор", price=125,
                                                attachment=NS(url="https://cdn.playerok.com/lot.webp"))))
        sent_event = NS(id="msg-4", text="{{ITEM_SENT}}", user=seller, images=[], created_at=None,
                        deal=NS(id="deal"))
        confirmed_event = NS(id="msg-5", text="{{ITEM_CONFIRMED}}", user=buyer, images=[], created_at=None,
                             deal=NS(id="deal"), event_by_user=buyer)
        chat = NS(id="thread", type="PM", status="FINISHED", users=[buyer, seller], is_texting_allowed=True,
                  unread_messages_counter=1, last_message=confirmed_event)
        elsewhere = NS(id="other", type="PM", users=[buyer])
        page_info = NS(has_next_page=True, end_cursor="older-page")
        pok = AsyncMock()
        pok.get_chats.return_value = NS(chats=[chat], page_info=page_info, total_count=1)
        pok.send_message.return_value = reply

        async def call(operation, chat_id, **kwargs):
            if operation == "get_chat":
                return chat if chat_id == "thread" else elsewhere
            if operation == "get_chat_messages":
                return NS(messages=[incoming, payment, sent_event, confirmed_event], page_info=page_info, total_count=4)
            return None

        pok.call.side_effect = call
        gateway = NS(get_client=AsyncMock(return_value=pok))
        app = MiniApp(db, gateway, plugins, TOKEN).create_app()
        async with TestClient(TestServer(app)) as client:
            health = await client.get("/healthz")
            assert await health.json() == {"status": "ok"}
            index = await client.get("/")
            assert "Emerald Desk" in await index.text()
            asset = await client.get("/assets/overview.webp")
            assert asset.status == 200
            assert (await asset.read())[:4] == b"RIFF"

            unauthorized = await client.get("/api/bootstrap")
            assert unauthorized.status == 401
            bootstrap = await client.get("/api/bootstrap", headers=authorize())
            assert (await bootstrap.json())["accounts"] == [{"id": str(own.id), "username": own.username}]
            assert bootstrap.headers["Cache-Control"] == "no-store"

            base = f"/api/accounts/{own.id}"
            for route, headers in (
                (f"/api/accounts/{foreign.id}/chats", authorize()),
                (f"/api/accounts/{foreign.id}/withdrawals", authorize()),
                (f"{base}/plugins", authorize(102)),
                (f"{base}/chats/other", authorize()),
            ):
                response = await client.get(route, headers=headers)
                assert response.status == 404

            response = await client.get(f"{base}/chats", headers=authorize())
            chat_data = (await response.json())["items"][0]
            assert chat_data["canReply"] is True
            assert chat_data["last"]["systemEvent"]["title"] == "Сделка завершена"
            thread = await client.get(f"{base}/chats/thread", headers=authorize())
            history = await thread.json()
            assert history["cursor"] == "older-page"
            assert history["items"][0]["text"] == "Купил товар"
            assert history["items"][1]["systemEvent"]["tone"] == "paid"
            assert history["items"][1]["item"]["price"] == "125"
            assert history["items"][1]["item"]["cover"] == "https://cdn.playerok.com/lot.webp"
            assert history["items"][2]["systemEvent"]["title"] == "Передача товара"
            assert history["items"][2]["item"]["name"] == "Набор"
            assert history["items"][3]["systemEvent"]["title"] == "Сделка завершена"
            assert history["items"][3]["item"]["price"] == "125"
            response = await client.post(f"{base}/chats/thread/reply", headers=authorize(), json={"text": "Ответ"})
            assert response.status == 201
            assert (await response.json())["mine"] is True
            pok.send_message.assert_awaited_once_with("thread", "Ответ", mark_chat_as_read=False)

            saved = await client.patch(f"{base}/plugins/example", headers=authorize(), json={
                "enabled": True, "settings": {"enabled_feature": False},
            })
            assert saved.status == 200
            result = await saved.json()
            assert result["enabled"]
            assert result["fields"][1]["configured"]
            assert "value" not in result["fields"][1]
            assert "must-not-leave-server" not in str(result)
            state, config = await plugins.resolved_state(own.id, plugin)
            assert state is True and config["enabled_feature"] is False
            bad = await client.patch(f"{base}/plugins/example", headers=authorize(), json={
                "settings": {"enabled_feature": "yes"},
            })
            assert bad.status == 400

            updated = await client.patch(f"{base}/notifications", headers=authorize(), json={"new_message": False})
            assert (await updated.json())["new_message"] is False
            assert (await (await client.get(f"{base}/notifications", headers=authorize())).json())["new_message"] is False
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_balance_listing_edits_and_telegram_settings_are_scoped(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/seller.db")
    await init_db(engine)
    db = make_session_factory(engine)
    try:
        async with db() as session:
            session.add(TelegramUser(id=101))
            await session.flush()
            account = PlayerokAccount(
                tg_user_id=101, playerok_user_id="seller-1", username="seller",
                cookies_encrypted="private", user_agent="test",
            )
            session.add(account)
            await session.flush()
            session.add(TelegramAccountStock(
                account_id=account.id, country="🇺🇸 США", phone="hidden-phone",
                session_encrypted="private-session",
            ))
            await session.commit()

        lot = NS(id="owned-lot", name="Свой лот", description="Описание", price=190, raw_price=190,
                 status="DRAFT", slug="owned", is_editable=True, attachments=[], user=NS(id="seller-1"))
        foreign = NS(id="foreign-lot", user=NS(id="someone-else"))
        pok = AsyncMock()
        pok.balance.return_value = {"available": "17.50", "value": 20, "frozen": 2.5}
        pok.get_item.side_effect = lambda item_id: {"owned-lot": lot, "foreign-lot": foreign}.get(item_id)
        pok.update_item.return_value = NS(**(vars(lot) | {"name": "Новое название", "price": 210}))
        gateway = NS(get_client=AsyncMock(return_value=pok))

        async with TestClient(TestServer(MiniApp(db, gateway, PluginManager(db), TOKEN).create_app())) as client:
            base = f"/api/accounts/{account.id}"
            headers = authorize()
            balance = await client.get(f"{base}/balance", headers=headers)
            assert (await balance.json())["available"] == "17.50"
            pok.call.return_value = NS(transactions=[
                NS(operation=TransactionOperations.WITHDRAW, user=NS(id="seller-1"),
                   status="CONFIRMED", value=500, fee=60, created_at=None,
                   props=NS(payment_account=NS(value="4111111111111111"))),
                NS(operation=TransactionOperations.WITHDRAW, user=NS(id="another-seller"),
                   status="PENDING", value=999),
            ], page_info=NS(has_next_page=False))
            withdrawal = await client.get(f"{base}/withdrawals", headers=headers)
            assert withdrawal.status == 200
            data = await withdrawal.json()
            assert len(data["items"]) == 1
            assert data["items"][0]["status"] == "CONFIRMED"
            assert data["items"][0]["details"] == "•••• 1111"
            assert "4111111111111111" not in str(data)
            assert data["cursor"] is None
            pok.call.assert_awaited_with("get_transactions", operation=TransactionOperations.WITHDRAW,
                                         count=10, after_cursor=None)
            rejected = await client.post(f"{base}/withdrawals", headers=headers, json={})
            assert rejected.status == 400
            assert "Подтвердите" in (await rejected.json())["error"]
            entry = await client.get(f"{base}/listings/owned-lot", headers=headers)
            assert (await entry.json())["name"] == "Свой лот"
            forbidden = await client.patch(f"{base}/listings/foreign-lot", headers=headers, json={"price": 1})
            assert forbidden.status == 404
            saved = await client.patch(f"{base}/listings/owned-lot", headers=headers, json={"name": "Новое название", "price": 210})
            assert saved.status == 200
            assert (await saved.json())["price"] == 210
            pok.update_item.assert_awaited_once_with("owned-lot", name="Новое название", price=210)
            payload = FormData()
            payload.add_field("image", b"\x89PNG\r\n\x1a\nimage", filename="cover.png", content_type="image/png")
            uploaded = await client.post(f"{base}/listings/owned-lot/photo", headers=headers, data=payload)
            assert uploaded.status == 200

            group = f"{base}/telegram-accounts"
            contents = await client.get(group, headers=headers)
            data = await contents.json()
            assert data["stock"][0]["available"] == 1
            assert "hidden-phone" not in str(data) and "private-session" not in str(data)
            secret = "a" * 32
            updated = await client.patch(group, headers=headers, json={"apiId": 4321, "apiHash": secret})
            stored = await updated.json()
            assert stored == {"apiId": 4321, "apiHashSet": True, "defaults": False}
            assert secret not in str(stored)
            wrong = await client.post(f"{group}/rules", headers=headers, json={"lotId": "foreign-lot", "country": "🇺🇸 США"})
            assert wrong.status == 404
            rule = await client.post(f"{group}/rules", headers=headers, json={"lotId": "owned-lot", "country": "🇺🇸 США"})
            assert rule.status == 201
            rule_path = f"{group}/rules/{(await rule.json())['id']}"
            changed = await client.patch(rule_path, headers=headers, json={"enabled": False, "country": "🇩🇪 Германия"})
            assert (await changed.json())["enabled"] is False
            result = await client.get(group, headers=headers)
            assert (await result.json())["rules"][0]["country"] == "🇩🇪 Германия"
            removed = await client.delete(rule_path, headers=headers)
            assert removed.status == 200
            key_rule = await client.post(f"{group}/rules", headers=headers, json={"keyword": "Telegram US", "country": "США"})
            assert key_rule.status == 201
            keyword_rule = await key_rule.json()
            assert keyword_rule["keyword"] == "Telegram US" and keyword_rule["lotId"] is None
            same_rule = await client.post(f"{group}/rules", headers=headers, json={"keyword": " telegram   us ", "country": "США"})
            assert (await same_rule.json())["id"] == keyword_rule["id"]
            invalid = await client.post(f"{group}/rules", headers=headers, json={"keyword": "  ", "country": "США"})
            assert invalid.status == 400
    finally:
        await engine.dispose()
