PLUGIN_META = {
    "id": "auto_reply",
    "name": "Автоответчик",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": "Отвечает на заданную фразу в чатах Playerok.",
    "settings": {
        "trigger": {"label": "Фраза", "type": "str", "default": "Здравствуйте"},
        "reply": {"label": "Ответ", "type": "str", "default": "Здравствуйте! Чем могу помочь?"},
    },
}


async def on_message(ctx, chat, message):
    text = str(getattr(message, "text", "") or "").casefold()
    trigger = str(ctx.config.get("trigger", "")).casefold().strip()
    if trigger and trigger in text:
        await ctx.send_chat(str(chat.id), str(ctx.config.get("reply", "")))
