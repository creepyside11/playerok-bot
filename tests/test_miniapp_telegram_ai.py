"""Настройки ИИ в Mini App: base url, токен, модель и базовый промпт."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.miniapp import MiniApp
from app.models import PlayerokAccount, TelegramUser
from app.plugin_system import PluginManager
from tests.test_miniapp import TOKEN, authorize

async def build(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/ai.db")
    await init_db(engine)
    db = make_session_factory(engine)
    async with db() as session:
        session.add(TelegramUser(id=101))
        await session.flush()
        account = PlayerokAccount(
            tg_user_id=101, playerok_user_id="seller", username="seller",
            cookies_encrypted="hidden", user_agent="test",
        )
        session.add(account)
        await session.commit()
    manager = PluginManager(db)
    manager.load()
    assert "ai_assistant" in manager.plugins
    app = MiniApp(db, NS(get_client=AsyncMock()), manager, TOKEN).create_app()
    return engine, db, manager, app, account

@pytest.mark.asyncio
async def test_ai_settings_are_editable_and_token_stays_secret(tmp_path):
    engine, db, manager, app, account = await build(tmp_path)
    try:
        async with TestClient(TestServer(app)) as client:
            headers = authorize()
            base = f"/api/accounts/{account.id}/telegram-accounts"

            assert (await client.get(base)).status == 401

            panel = await (await client.get(base, headers=headers)).json()
            ai = panel["ai"]
            assert ai["available"] is True
            assert ai["enabled"] is False
            assert ai["tokenSet"] is False
            assert ai["baseUrl"] == "https://api.anthropic.com"
            assert ai["mode"] == "help_command_only"
            assert "always_after_help" in ai["modes"]
            assert "api_key" not in str(ai)

            saved = await client.patch(base + "/ai", headers=headers, json={
                "enabled": True,
                "baseUrl": "https://openrouter.ai/api",
                "apiToken": "sk-secret-value",
                "model": "anthropic/claude-3.5",
                "basePrompt": "Отвечай кратко и по делу.",
                "mode": "always_after_help",
                "notifyTg": False,
            })
            assert saved.status == 200
            payload = await saved.json()
            assert payload["enabled"] is True
            assert payload["tokenSet"] is True
            assert payload["baseUrl"] == "https://openrouter.ai/api"
            assert payload["model"] == "anthropic/claude-3.5"
            assert payload["basePrompt"] == "Отвечай кратко и по делу."
            assert payload["mode"] == "always_after_help"
            assert payload["notifyTg"] is False
            assert "sk-secret-value" not in str(payload), "токен не должен возвращаться в API"

            # Пустой токен означает «не менять»: остальные поля сохраняются.
            kept = await (await client.patch(base + "/ai", headers=headers, json={
                "apiToken": "", "model": "claude-4",
            })).json()
            assert kept["tokenSet"] is True and kept["model"] == "claude-4"

            reset = await (await client.patch(base + "/ai", headers=headers, json={"resetToken": True})).json()
            assert reset["tokenSet"] is False

            enabled, config = await manager.resolved_state(account.id, manager.plugins["ai_assistant"])
            assert enabled is True
            assert config["api_key"] == ""
            assert config["base_url"] == "https://openrouter.ai/api"
            assert config["base_prompt"] == "Отвечай кратко и по делу."
    finally:
        await engine.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"mode": "неизвестный"},
    {"baseUrl": "ftp://evil"},
    {"baseUrl": "   "},
    {"enabled": "yes"},
    {"notifyTg": "true"},
    {"apiToken": 5},
    {"basePrompt": "x" * 8001},
    {"unknown": 1},
    {"resetToken": True, "model": "x"},
])
async def test_ai_settings_reject_bad_input(tmp_path, body):
    engine, db, manager, app, account = await build(tmp_path)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.patch(
                f"/api/accounts/{account.id}/telegram-accounts/ai", headers=authorize(), json=body,
            )
            assert response.status == 400
            enabled, config = await manager.resolved_state(account.id, manager.plugins["ai_assistant"])
            assert config["base_url"] == "https://api.anthropic.com"
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_ai_settings_are_scoped_to_the_account(tmp_path):
    engine, db, manager, app, account = await build(tmp_path)
    try:
        async with db() as session:
            session.add(TelegramUser(id=202))
            await session.flush()
            foreign = PlayerokAccount(
                tg_user_id=202, playerok_user_id="other", username="other",
                cookies_encrypted="hidden", user_agent="test",
            )
            session.add(foreign)
            await session.commit()
        async with TestClient(TestServer(app)) as client:
            response = await client.patch(
                f"/api/accounts/{foreign.id}/telegram-accounts/ai",
                headers=authorize(), json={"model": "stolen"},
            )
            assert response.status == 404
    finally:
        await engine.dispose()
