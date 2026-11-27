"""#продавец: крупное закреплённое сообщение продавцу и ответ покупателю."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.telegram_accounts_manager import format_seller_called_message
from plugins import telegram_accounts

CHAT = "chat-42"

def make_ctx():
    return NS(
        account=NS(id="acc", playerok_user_id="seller", tg_user_id=10),
        send_chat=AsyncMock(),
        notify=AsyncMock(),
        call_seller=AsyncMock(),
    )

def message(text, username="buyer"):
    return NS(text=text, user=NS(username=username))

def chat():
    return NS(id=CHAT)

@pytest.fixture(autouse=True)
def reset_cooldowns():
    telegram_accounts._seller_calls.clear()
    yield
    telegram_accounts._seller_calls.clear()

@pytest.mark.asyncio
async def test_seller_command_answers_buyer_and_pins_for_seller():
    ctx = make_ctx()
    await telegram_accounts.on_message(ctx, chat(), message("#продавец Почта не подходит"))
    ctx.send_chat.assert_awaited_once_with(CHAT, format_seller_called_message())
    ctx.call_seller.assert_awaited_once_with(
        CHAT, buyer="buyer", question="Почта не подходит",
    )

@pytest.mark.asyncio
async def test_seller_command_works_without_question():
    ctx = make_ctx()
    await telegram_accounts.on_message(ctx, chat(), message("#продавец"))
    ctx.call_seller.assert_awaited_once_with(CHAT, buyer="buyer", question="")

@pytest.mark.asyncio
async def test_repeated_seller_command_is_throttled():
    ctx = make_ctx()
    for _ in range(3):
        await telegram_accounts.on_message(ctx, chat(), message("#продавец"))
    assert ctx.call_seller.await_count == 1
    assert ctx.send_chat.await_count == 1

@pytest.mark.asyncio
async def test_cooldown_expires(monkeypatch):
    ctx = make_ctx()
    await telegram_accounts.on_message(ctx, chat(), message("#продавец"))
    monkeypatch.setattr(telegram_accounts, "SELLER_CALL_COOLDOWN", 0.0)
    await telegram_accounts.on_message(ctx, chat(), message("#продавец"))
    assert ctx.call_seller.await_count == 2

@pytest.mark.asyncio
async def test_other_commands_do_not_call_seller():
    ctx = make_ctx()
    for text in ("#аск помогите", "привет", "#код123", ""):
        await telegram_accounts.on_message(ctx, chat(), message(text))
    ctx.call_seller.assert_not_awaited()
    ctx.send_chat.assert_not_awaited()

@pytest.mark.asyncio
async def test_seller_alias_is_supported():
    ctx = make_ctx()
    await telegram_accounts.on_message(ctx, chat(), message("#человек"))
    ctx.call_seller.assert_awaited_once()
