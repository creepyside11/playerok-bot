"""Клиент Telethon переиспользуется: повторный #код не должен ждать новый коннект."""
import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app import telegram_accounts_manager as manager

NOW = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)

class FakeClient:
    instances = 0

    def __init__(self, *args, **kwargs):
        FakeClient.instances += 1
        self.dialogs = []
        self.connected = False
        self.authorized = True
        self.messages_for = {}
        self.get_dialogs_calls = 0
        self.get_messages_calls = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized

    async def get_dialogs(self, limit=10):
        self.get_dialogs_calls += 1
        return self.dialogs[:limit]

    async def get_messages(self, entity, limit=3):
        self.get_messages_calls += 1
        return self.messages_for.get(entity, [])[:limit]

def dialog(text, minutes_ago, entity="tg"):
    return NS(message=NS(message=text, date=NOW - timedelta(minutes=minutes_ago)), entity=entity)

@pytest.fixture
def fake_telethon(monkeypatch):
    FakeClient.instances = 0
    created: list[FakeClient] = []

    class Tracking(FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = Tracking
    sessions = types.ModuleType("telethon.sessions")
    sessions.StringSession = lambda value: value
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)
    monkeypatch.setattr(manager, "_CLIENTS", {})
    monkeypatch.setattr(manager, "_CLIENT_LOCKS", {})
    yield created

@pytest.mark.asyncio
async def test_client_is_reused_between_requests(fake_telethon):
    async def run():
        first = await manager.fetch_latest_code_from_telethon("session-1", 2040, "hash")
        client = fake_telethon[0]
        client.dialogs = [dialog("Telegram code: 12345", 1)]
        second = await manager.fetch_latest_code_from_telethon("session-1", 2040, "hash")
        return first, second

    # Первый вызов: диалогов нет, значит код не найден, но клиент уже создан.
    first, second = await run()
    assert first == (None, "no_messages")
    assert second == ("12345", "Telegram code: 12345")
    assert len(fake_telethon) == 1, "клиент должен переиспользоваться, а не переподключаться"

@pytest.mark.asyncio
async def test_newest_message_is_enough_without_extra_fetch(fake_telethon):
    await manager.fetch_latest_code_from_telethon("s", 1, "h")
    client = fake_telethon[0]
    client.dialogs = [
        dialog("просто болтовня", 5, entity="a"),
        dialog("Login code: 54321. Do not share.", 1, entity="b"),
    ]
    code, raw = await manager.fetch_latest_code_from_telethon("s", 1, "h")
    assert code == "54321"
    assert client.get_messages_calls == 0, "код в последнем сообщении не требует лишних запросов"

@pytest.mark.asyncio
async def test_falls_back_to_parallel_scan(fake_telethon):
    await manager.fetch_latest_code_from_telethon("s", 1, "h")
    client = fake_telethon[0]
    client.dialogs = [dialog("свежее сообщение без кода", 1, entity="a")]
    client.messages_for = {"a": [NS(message="Код подтверждения: 98765")]}
    code, _ = await manager.fetch_latest_code_from_telethon("s", 1, "h")
    assert code == "98765"

@pytest.mark.asyncio
async def test_timeout_returns_quickly_and_drops_client(fake_telethon, monkeypatch):
    monkeypatch.setattr(manager, "CODE_FETCH_TIMEOUT", 0.01)
    await manager.fetch_latest_code_from_telethon("slow", 1, "h")
    client = fake_telethon[0]

    async def hang(limit=10):
        await asyncio.sleep(5)
        return []

    client.get_dialogs = hang
    code, reason = await manager.fetch_latest_code_from_telethon("slow", 1, "h")
    assert code is None and reason == "timeout"
    assert manager._CLIENTS == {}, "после таймаута клиент выбрасывается из кеша"

@pytest.mark.asyncio
async def test_unauthorized_session_is_reported(fake_telethon):
    await manager.fetch_latest_code_from_telethon("s", 1, "h")
    await manager.reset_telethon_cache()

    async def unauthorized(self):
        return False

    fake_telethon[0].__class__.is_user_authorized = unauthorized
    code, reason = await manager.fetch_latest_code_from_telethon("s", 1, "h")
    assert code is None and "unauthorized" in reason
