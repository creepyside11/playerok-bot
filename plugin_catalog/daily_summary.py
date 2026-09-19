PLUGIN_META = {
    "id": "daily_summary",
    "name": "Ежедневная сводка",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": "Напоминает владельцу, что можно проверить заказы и чаты.",
    "settings": {"hour": {"label": "Час уведомления", "type": "int", "default": 12}},
}


async def on_schedule(ctx):
    await ctx.notify("📊 Ежедневная сводка: проверьте новые заказы, чаты и баланс Playerok.")
