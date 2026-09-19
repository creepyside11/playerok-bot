PLUGIN_META = {
    "id": "order_notifier",
    "name": "Уведомления о заказах",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": "Отправляет уведомление в Telegram о новых заказах Playerok.",
    "settings": {"enabled": {"label": "Уведомления", "type": "bool", "default": True}},
}


async def on_deal(ctx, deal):
    if ctx.config.get("enabled", True):
        title = getattr(deal, "title", None) or getattr(deal, "id", "новый заказ")
        await ctx.notify(f"🛒 Новый заказ Playerok: {title}")
