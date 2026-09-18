from __future__ import annotations

from typing import Any


PLUGIN_META = {
    "id": "example_echo",
    "name": "Example Echo",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": "Пример плагина: отвечает на заданную команду в Playerok-чате.",
    "settings": {
        "trigger": {
            "label": "Команда",
            "type": "str",
            "default": "!ping",
        },
        "reply": {
            "label": "Ответ",
            "type": "str",
            "default": "pong",
        },
        "notify_tg": {
            "label": "Уведомлять в Telegram",
            "type": "bool",
            "default": False,
        },
    },
}


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    text = str(getattr(message, "text", "") or "").strip()
    trigger = str(ctx.config.get("trigger") or "").strip()
    if not trigger or text.casefold() != trigger.casefold():
        return

    reply = str(ctx.config.get("reply") or "pong")
    await ctx.send_chat(str(chat.id), reply)

    if ctx.config.get("notify_tg"):
        sender = getattr(getattr(message, "user", None), "username", "пользователь")
        await ctx.notify(f"Example Echo сработал для @{sender}")


async def on_deal(ctx: Any, deal: Any) -> None:
    # Необязательный обработчик новых заказов.
    return None
