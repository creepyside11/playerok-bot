"""Крупное сообщение «нужен продавец» закрепляется в чате Telegram."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.plugin_system import PluginContext

def build(bot):
    return PluginContext(
        account=NS(id="acc", tg_user_id=555, playerok_user_id="seller"),
        client=NS(), bot=bot, db=None, config={},
    )

@pytest.mark.asyncio
async def test_notify_pinned_sends_and_pins():
    bot = NS(send_message=AsyncMock(return_value=NS(message_id=77)), pin_chat_message=AsyncMock())
    await build(bot).notify_pinned("текст")
    bot.send_message.assert_awaited_once()
    bot.pin_chat_message.assert_awaited_once_with(555, 77, disable_notification=False)

@pytest.mark.asyncio
async def test_notify_pinned_survives_pin_failure():
    bot = NS(
        send_message=AsyncMock(return_value=NS(message_id=78)),
        pin_chat_message=AsyncMock(side_effect=RuntimeError("no rights")),
    )
    message = await build(bot).notify_pinned("текст")
    assert message.message_id == 78

@pytest.mark.asyncio
async def test_notify_pinned_skips_pin_without_message_id():
    bot = NS(send_message=AsyncMock(return_value=NS()), pin_chat_message=AsyncMock())
    await build(bot).notify_pinned("текст")
    bot.pin_chat_message.assert_not_awaited()

@pytest.mark.asyncio
async def test_call_seller_message_is_large_and_escaped():
    bot = NS(send_message=AsyncMock(return_value=NS(message_id=1)), pin_chat_message=AsyncMock())
    await build(bot).call_seller("chat-9", buyer="<script>", question="Где код?")
    text = bot.send_message.await_args.args[1]
    assert "НУЖЕН ПРОДАВЕЦ" in text
    assert "chat-9" in text
    assert "Где код?" in text
    assert "<script>" not in text, "имя покупателя экранируется"
    assert "&lt;script&gt;" in text
    bot.pin_chat_message.assert_awaited_once()

@pytest.mark.asyncio
async def test_call_seller_without_question():
    bot = NS(send_message=AsyncMock(return_value=NS(message_id=2)), pin_chat_message=AsyncMock())
    await build(bot).call_seller("chat-1")
    text = bot.send_message.await_args.args[1]
    assert "Вопрос" not in text
    assert "Покупатель" in text
