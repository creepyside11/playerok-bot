"""Обе плагина получают каждое сообщение: команды не должны конфликтовать."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import PlayerokAccount, TelegramUser
from plugins import ai_assistant, telegram_accounts

CHAT = "chat-1"

def message(text, username="buyer"):
    return NS(text=text, user=NS(username=username), item=None, deal=None)

def chat():
    return NS(id=CHAT, deals=[])

@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    ai_assistant._ACTIVE_AI_CHATS.clear()
    telegram_accounts._seller_calls.clear()

    async def reply(self, model, system_prompt, messages, max_tokens=1000):
        return "Ответ помощника."

    monkeypatch.setattr(ai_assistant.AnthropicBridge, "create_message", reply)
    yield
    ai_assistant._ACTIVE_AI_CHATS.clear()
    telegram_accounts._seller_calls.clear()

async def seed(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/routing.db")
    await init_db(engine)
    factory = make_session_factory(engine)
    async with factory() as session:
        session.add(TelegramUser(id=10))
        await session.flush()
        account = PlayerokAccount(
            tg_user_id=10, playerok_user_id="seller", username="seller",
            cookies_encrypted="fixture", user_agent="test",
        )
        session.add(account)
        await session.commit()
    return engine, factory, account

def build(db, account, ai_mode="always_after_help"):
    """Оба контекста смотрят в один чат; считаем, кто именно ответил."""
    tg_ctx = NS(
        account=NS(id=account.id, tg_user_id=10, playerok_user_id="seller"),
        db=db,
        config={},
        client=NS(send_message=AsyncMock(return_value=NS(id="sent")), call=AsyncMock()),
        notify=AsyncMock(),
        send_chat=AsyncMock(),
        call_seller=AsyncMock(),
    )
    ai_ctx = NS(
        account=tg_ctx.account,
        db=db,
        config={
            "api_key": "sk", "base_url": "https://api.anthropic.com", "model": "claude",
            "base_prompt": "Ты помощник.", "mode": ai_mode, "notify_tg_on_reply": False,
        },
        client=tg_ctx.client,
        send_chat=AsyncMock(),
        notify=AsyncMock(),
        call_seller=AsyncMock(),
    )
    return tg_ctx, ai_ctx

async def deliver(tg_ctx, ai_ctx, text):
    """Повторяет порядок диспетчера плагинов: оба хука на каждое сообщение.

    Возвращает число ответов, отправленных именно на это сообщение.
    """
    before = (tg_ctx.send_chat.await_count, ai_ctx.send_chat.await_count)
    await telegram_accounts.on_message(tg_ctx, chat(), message(text))
    await ai_assistant.on_message(ai_ctx, chat(), message(text))
    return tg_ctx.send_chat.await_count - before[0], ai_ctx.send_chat.await_count - before[1]

@pytest.mark.asyncio
async def test_ask_is_answered_only_by_assistant(tmp_path):
    engine, factory, account = await seed(tmp_path)
    try:
        tg_ctx, ai_ctx = build(factory, account)
        tg_sent, ai_sent = await deliver(tg_ctx, ai_ctx, "#аск Как активировать?")
        assert (tg_sent, ai_sent) == (0, 1)
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_code_command_is_not_answered_by_assistant(tmp_path):
    engine, factory, account = await seed(tmp_path)
    try:
        tg_ctx, ai_ctx = build(factory, account)
        tg_sent, ai_sent = await deliver(tg_ctx, ai_ctx, "#код")
        assert ai_sent == 0, "помощник не должен перехватывать #код"
        assert tg_sent == 0, "без активной сделки плагин молчит"
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_seller_command_goes_to_stock_plugin(tmp_path):
    engine, factory, account = await seed(tmp_path)
    try:
        tg_ctx, ai_ctx = build(factory, account)
        _tg_sent, ai_sent = await deliver(tg_ctx, ai_ctx, "#продавец")
        assert ai_sent == 0
        tg_ctx.call_seller.assert_awaited_once()
        ai_ctx.call_seller.assert_not_awaited()
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_stop_is_answered_once(tmp_path):
    engine, factory, account = await seed(tmp_path)
    try:
        tg_ctx, ai_ctx = build(factory, account)
        await deliver(tg_ctx, ai_ctx, "#аск Привет")
        tg_sent, ai_sent = await deliver(tg_ctx, ai_ctx, "#аскстоп")
        assert (tg_sent, ai_sent) == (0, 1)
        assert CHAT not in ai_assistant._ACTIVE_AI_CHATS
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_dialog_follow_up_does_not_disturb_stock_plugin(tmp_path):
    engine, factory, account = await seed(tmp_path)
    try:
        tg_ctx, ai_ctx = build(factory, account)
        await deliver(tg_ctx, ai_ctx, "#аск Привет")
        tg_sent, ai_sent = await deliver(tg_ctx, ai_ctx, "А сколько ждать?")
        assert (tg_sent, ai_sent) == (0, 1)
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_plain_message_is_silent_in_command_mode(tmp_path):
    engine, factory, account = await seed(tmp_path)
    try:
        tg_ctx, ai_ctx = build(factory, account, ai_mode="help_command_only")
        tg_sent, ai_sent = await deliver(tg_ctx, ai_ctx, "Здравствуйте, где товар?")
        assert (tg_sent, ai_sent) == (0, 0)
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_exit_commands_are_not_answered_by_assistant(tmp_path):
    """#выход, #да и #нет — служебные: помощник обязан молчать."""
    engine, factory, account = await seed(tmp_path)
    try:
        tg_ctx, ai_ctx = build(factory, account, ai_mode="all_messages")
        for text in ("#выход", "#да", "#нет"):
            _tg, ai_sent = await deliver(tg_ctx, ai_ctx, text)
            assert ai_sent == 0, text
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_ask_still_works_while_exit_pending(tmp_path):
    engine, factory, account = await seed(tmp_path)
    try:
        tg_ctx, ai_ctx = build(factory, account)
        await deliver(tg_ctx, ai_ctx, "#выход")
        _tg, ai_sent = await deliver(tg_ctx, ai_ctx, "#аск Что дальше?")
        assert ai_sent == 1
    finally:
        await engine.dispose()
