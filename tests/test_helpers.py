import asyncio

import pytest
from sqlalchemy.exc import OperationalError

import app.db as db_module
from app.config import DEFAULT_DATABASE_URL, Settings
from app.playerok import normalize_proxy, parse_cookie_header


def test_database_defaults_to_docker_sqlite(monkeypatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("Database_URL", raising=False)
    monkeypatch.delenv("database_url", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "test-token")

    assert Settings.from_env().database_url == DEFAULT_DATABASE_URL
    assert DEFAULT_DATABASE_URL == "sqlite+aiosqlite:///playerok.db"


def test_postgres_engine_sets_asyncpg_connection_timeout(monkeypatch) -> None:
    captured = {}
    engine = object()

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return engine

    monkeypatch.setattr(db_module, "create_async_engine", fake_create_async_engine)

    assert db_module.make_engine("postgresql+asyncpg://user:pass@db.example/playerok") is engine
    assert captured["connect_args"] == {"timeout": 15.0}
    assert captured["pool_pre_ping"] is True


def test_db_init_retries_then_succeeds(monkeypatch) -> None:
    class FakeEngine:
        pass

    async def run() -> None:
        calls = 0
        delays = []

        async def fake_init(_engine) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise OperationalError("connect", {}, TimeoutError("temporary database timeout"))

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        monkeypatch.setattr(db_module, "init_db", fake_init)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await db_module.init_db_with_retry(FakeEngine(), attempts=4, initial_delay=0.25)

        assert calls == 3
        assert delays == [0.25, 0.5]

    asyncio.run(run())


def test_db_init_does_not_retry_configuration_errors(monkeypatch) -> None:
    class FakeEngine:
        pass

    async def run() -> None:
        calls = 0
        delays = []

        async def fake_init(_engine) -> None:
            nonlocal calls
            calls += 1
            raise OperationalError("init", {}, Exception("authentication failed"))

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        monkeypatch.setattr(db_module, "init_db", fake_init)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        with pytest.raises(OperationalError):
            await db_module.init_db_with_retry(FakeEngine(), attempts=4, initial_delay=0.25)

        assert calls == 1
        assert not delays

    asyncio.run(run())


def test_proxy_normalization() -> None:
    assert normalize_proxy("1.2.3.4:8000") == "1.2.3.4:8000"
    assert normalize_proxy("1.2.3.4:8000:user:pass") == "user:pass@1.2.3.4:8000"
    assert normalize_proxy("-") is None


def test_cookie_parser() -> None:
    assert parse_cookie_header("token=abc; x=1") == {"token": "abc", "x": "1"}
