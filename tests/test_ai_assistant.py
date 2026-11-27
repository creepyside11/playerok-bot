import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from plugins import ai_assistant

CHAT = "chat-1"

def make_ctx(config=None):
    values = {
        "api_key": "sk-test", "base_url": "https://api.anthropic.com", "model": "claude-test",
        "base_prompt": "Ты помощник продавца.", "mode": "help_command_only", "notify_tg_on_reply": True,
    }
    values.update(config or {})
    ctx = NS(
        account=NS(id="acc", playerok_user_id="seller", tg_user_id=10),
        config=values,
        send_chat=AsyncMock(),
        notify=AsyncMock(),
        call_seller=AsyncMock(),
    )
    return ctx

def message(text, username="buyer"):
    return NS(text=text, user=NS(username=username), item=None, deal=None)

def chat():
    return NS(id=CHAT, deals=[])

@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    ai_assistant._ACTIVE_AI_CHATS.clear()
    replies = []

    async def fake_create(self, model, system_prompt, messages, max_tokens=1000):
        replies.append({"model": model, "system": system_prompt, "messages": messages})
        return "Здравствуйте! Код приходит в этот чат."

    monkeypatch.setattr(ai_assistant.AnthropicBridge, "create_message", fake_create)
    yield replies
    ai_assistant._ACTIVE_AI_CHATS.clear()

@pytest.mark.asyncio
async def test_ask_command_replies_and_opens_dialog(clean_state):
    ctx = make_ctx()
    await ai_assistant.on_message(ctx, chat(), message("#аск Как активировать?"))
    ctx.send_chat.assert_awaited_once_with(CHAT, "Здравствуйте! Код приходит в этот чат.")
    assert CHAT in ai_assistant._ACTIVE_AI_CHATS
    assert clean_state[0]["messages"][0]["content"] == "Как активировать?"
    ctx.call_seller.assert_not_awaited()

@pytest.mark.asyncio
async def test_bare_ask_uses_default_question(clean_state):
    ctx = make_ctx()
    await ai_assistant.on_message(ctx, chat(), message("#аск"))
    ctx.send_chat.assert_awaited_once()
    assert clean_state[0]["messages"][0]["content"] == ai_assistant.DEFAULT_QUESTION

@pytest.mark.asyncio
async def test_stop_command_ends_dialog(clean_state):
    ctx = make_ctx({"mode": "always_after_help"})
    await ai_assistant.on_message(ctx, chat(), message("#аск Привет"))
    assert CHAT in ai_assistant._ACTIVE_AI_CHATS
    await ai_assistant.on_message(ctx, chat(), message("#аскстоп"))
    assert CHAT not in ai_assistant._ACTIVE_AI_CHATS
    assert ai_assistant.STOP_MESSAGE in ctx.send_chat.await_args.args[1]
    before = ctx.send_chat.await_count
    await ai_assistant.on_message(ctx, chat(), message("А теперь кто ответит?"))
    assert ctx.send_chat.await_count == before

@pytest.mark.asyncio
async def test_other_commands_keep_working_during_dialog(clean_state):
    """#код и #продавец не должны перехватываться помощником в режиме диалога."""
    ctx = make_ctx({"mode": "all_messages"})
    await ai_assistant.on_message(ctx, chat(), message("#аск Привет"))
    sent = ctx.send_chat.await_count
    for command in ("#код", "#продавец", "#код пожалуйста"):
        await ai_assistant.on_message(ctx, chat(), message(command))
    assert ctx.send_chat.await_count == sent
    assert len(clean_state) == 1

@pytest.mark.asyncio
async def test_plain_message_is_ignored_in_command_mode(clean_state):
    ctx = make_ctx()
    await ai_assistant.on_message(ctx, chat(), message("Здравствуйте, где мой товар?"))
    ctx.send_chat.assert_not_awaited()
    assert clean_state == []

@pytest.mark.asyncio
async def test_dialog_mode_answers_follow_ups(clean_state):
    ctx = make_ctx({"mode": "always_after_help"})
    await ai_assistant.on_message(ctx, chat(), message("#аск Привет"))
    await ai_assistant.on_message(ctx, chat(), message("А сколько ждать?"))
    assert ctx.send_chat.await_count == 2
    assert clean_state[1]["messages"][0]["content"] == "А сколько ждать?"

@pytest.mark.asyncio
async def test_seller_tag_strips_from_reply_and_calls_seller(clean_state, monkeypatch):
    async def ask_seller(self, model, system_prompt, messages, max_tokens=1000):
        return "Сейчас позову продавца. [CALL_SELLER]"

    monkeypatch.setattr(ai_assistant.AnthropicBridge, "create_message", ask_seller)
    ctx = make_ctx()
    await ai_assistant.on_message(ctx, chat(), message("#аск Верните деньги"))
    ctx.send_chat.assert_awaited_once_with(CHAT, "Сейчас позову продавца.")
    ctx.call_seller.assert_awaited_once()
    assert ctx.call_seller.await_args.kwargs["question"] == "Верните деньги"
    assert "CALL_SELLER" not in ctx.send_chat.await_args.args[1]

@pytest.mark.asyncio
async def test_unconfigured_assistant_calls_seller(clean_state):
    ctx = make_ctx({"api_key": "", "model": ""})
    await ai_assistant.on_message(ctx, chat(), message("#аск Помогите"))
    assert ctx.send_chat.await_args.args[1] == ai_assistant.UNAVAILABLE_MESSAGE
    ctx.call_seller.assert_awaited_once()
    assert clean_state == []

@pytest.mark.asyncio
async def test_api_failure_notifies_seller(clean_state, monkeypatch):
    async def boom(self, model, system_prompt, messages, max_tokens=1000):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(ai_assistant.AnthropicBridge, "create_message", boom)
    ctx = make_ctx()
    await ai_assistant.on_message(ctx, chat(), message("#аск Привет"))
    assert "техническая ошибка" in ctx.send_chat.await_args.args[1]
    assert "HTTP 500" in ctx.notify.await_args.args[0]

@pytest.mark.asyncio
async def test_blank_message_is_ignored(clean_state):
    ctx = make_ctx()
    await ai_assistant.on_message(ctx, chat(), message("   "))
    ctx.send_chat.assert_not_awaited()

@pytest.mark.asyncio
async def test_passive_mode_failure_does_not_spam_seller(clean_state, monkeypatch):
    """В режиме «все сообщения» сбой API не должен дёргать продавца на каждую реплику."""
    async def boom(self, model, system_prompt, messages, max_tokens=1000):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(ai_assistant.AnthropicBridge, "create_message", boom)
    ctx = make_ctx({"mode": "all_messages"})
    await ai_assistant.on_message(ctx, chat(), message("А сколько ждать?"))
    ctx.send_chat.assert_not_awaited()
    ctx.notify.assert_not_awaited()
    ctx.call_seller.assert_not_awaited()

@pytest.mark.asyncio
async def test_passive_mode_without_key_stays_silent(clean_state):
    ctx = make_ctx({"api_key": "", "model": "", "mode": "all_messages"})
    await ai_assistant.on_message(ctx, chat(), message("А сколько ждать?"))
    ctx.send_chat.assert_not_awaited()
    ctx.call_seller.assert_not_awaited()

@pytest.mark.asyncio
async def test_unconfigured_ask_still_answers_and_calls_seller(clean_state):
    ctx = make_ctx({"api_key": "", "model": ""})
    await ai_assistant.on_message(ctx, chat(), message("#аск Когда код?"))
    ctx.send_chat.assert_awaited_once_with(CHAT, ai_assistant.UNAVAILABLE_MESSAGE)
    ctx.call_seller.assert_awaited_once()
