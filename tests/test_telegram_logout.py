"""Telethon-хелперы выхода: список устройств и auth.logOut."""
import sys
import types
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app import telegram_accounts_manager as manager

class FakeAuth:
    def __init__(self, hash, current=None, device_model="", platform="", app_name=""):
        self.hash = hash
        self.current = current
        self.device_model = device_model
        self.platform = platform
        self.app_name = app_name

class FakeClient:
    created: list = []

    def __init__(self, *args, **kwargs):
        self.connected = False
        self.authorized = True
        self.authorizations = []
        self.logout_result = True
        self.raise_on_logout = None
        self.calls = []
        FakeClient.created.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized

    async def get_dialogs(self, limit=10):
        return []

    async def log_out(self):
        self.calls.append("log_out")
        if self.raise_on_logout:
            raise self.raise_on_logout
        return self.logout_result

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        return NS(authorizations=self.authorizations)

@pytest.fixture
def fake_telethon(monkeypatch):
    FakeClient.created = []
    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = FakeClient
    sessions = types.ModuleType("telethon.sessions")
    sessions.StringSession = lambda value: value
    tl = types.ModuleType("telethon.tl")
    functions = types.ModuleType("telethon.tl.functions")
    account = types.ModuleType("telethon.tl.functions.account")

    class GetAuthorizationsRequest:
        pass

    account.GetAuthorizationsRequest = GetAuthorizationsRequest
    functions.account = account
    tl.functions = functions
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)
    monkeypatch.setitem(sys.modules, "telethon.tl", tl)
    monkeypatch.setitem(sys.modules, "telethon.tl.functions", functions)
    monkeypatch.setitem(sys.modules, "telethon.tl.functions.account", account)
    monkeypatch.setattr(manager, "_CLIENTS", {})
    monkeypatch.setattr(manager, "_CLIENT_LOCKS", {})
    yield FakeClient.created

@pytest.mark.asyncio
async def test_list_authorizations_maps_entries(fake_telethon):
    await manager.fetch_latest_code_from_telethon("s", 1, "h")
    client = fake_telethon[0]
    client.authorizations = [
        FakeAuth(111, current=True, device_model="Pixel", platform="Android", app_name="Telegram"),
        FakeAuth(222, device_model="iPhone", platform="iOS", app_name="Telegram"),
    ]
    entries = await manager.list_authorizations("s", 1, "h")
    assert [entry["hash"] for entry in entries] == [111, 222]
    assert [entry["current"] for entry in entries] == [True, False]
    assert entries[1]["device"] == "iPhone"
    assert "GetAuthorizationsRequest" in client.calls

@pytest.mark.asyncio
async def test_list_authorizations_returns_none_on_error(fake_telethon, monkeypatch):
    async def boom(request):
        raise RuntimeError("network")

    await manager.fetch_latest_code_from_telethon("s", 1, "h")
    fake_telethon[0].__class__.__call__ = boom
    assert await manager.list_authorizations("s", 1, "h") is None

@pytest.mark.asyncio
async def test_logout_calls_log_out_and_clears_cache(fake_telethon):
    await manager.fetch_latest_code_from_telethon("s", 1, "h")
    client = fake_telethon[0]
    assert await manager.logout_telethon_session("s", 1, "h") is True
    assert client.calls == ["log_out"]
    assert manager._CLIENTS == {}, "после выхода клиент не должен оставаться в кеше"

@pytest.mark.asyncio
async def test_logout_returns_false_when_rpc_refuses(fake_telethon):
    await manager.fetch_latest_code_from_telethon("s", 1, "h")
    fake_telethon[0].logout_result = False
    assert await manager.logout_telethon_session("s", 1, "h") is False

@pytest.mark.asyncio
async def test_logout_survives_exception(fake_telethon):
    await manager.fetch_latest_code_from_telethon("s", 1, "h")
    fake_telethon[0].raise_on_logout = RuntimeError("boom")
    assert await manager.logout_telethon_session("s", 1, "h") is False
    assert manager._CLIENTS == {}

@pytest.mark.asyncio
async def test_logout_creates_client_when_cache_empty(fake_telethon):
    assert await manager.logout_telethon_session("fresh", 1, "h") is True
    assert len(fake_telethon) == 1

@pytest.mark.parametrize("entries,expected", [
    ([{"current": True}, {"current": False}], True),
    ([{"current": False}, {"current": True}], True),
    ([{"current": True}], False),
    ([{"current": True}, {"current": True}], False),
    ([{"current": False}, {"current": False}], True),
    ([], False),
    (None, False),
])
def test_buyer_session_present(entries, expected):
    assert manager.buyer_session_present(entries) is expected
