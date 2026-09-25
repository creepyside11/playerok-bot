# Документация плагинов Playerok Bot

Этот файл можно скачать и отправить в ИИ вместе с примером плагина.

## Установка

1. Создайте файл `plugins/my_plugin.py`.
2. Отправьте его боту в разделе **🧩 Плагины → 📤 Загрузить плагин** как документ.
3. Включите плагин и настройте параметры.

Файлы должны быть Python `.py`, максимум 256 КБ. Плагины выполняются с правами процесса бота: загружайте только доверенный код.

## Минимальный плагин

```python
PLUGIN_META = {
    "id": "welcome_reply",
    "name": "Welcome Reply",
    "version": "1.0.0",
    "author": "Ваше имя",
    "description": "Отвечает на сообщения с ключевым словом.",
    "settings": {
        "keyword": {"label": "Ключевое слово", "type": "str", "default": "привет"},
        "reply": {"label": "Ответ", "type": "str", "default": "Здравствуйте!"},
        "enabled": {"label": "Активен", "type": "bool", "default": True},
        "limit": {"label": "Лимит", "type": "int", "default": 3},
        "mode": {"label": "Режим", "type": "choice", "choices": ["short", "full"], "default": "short"},
    },
}

async def on_message(ctx, chat, message):
    text = str(getattr(message, "text", "") or "")
    if ctx.config["keyword"].casefold() in text.casefold():
        await ctx.send_chat(getattr(chat, "id"), ctx.config["reply"])
        await ctx.notify(f"Плагин ответил в чате {getattr(chat, 'id', '')}")
```

## Метаданные

- `id` — уникальный идентификатор: латинские буквы, цифры, `.`, `_`, `-`, до 32 символов.
- `name`, `version`, `author`, `description` — информация в интерфейсе.
- `settings` — настройки аккаунта. Типы: `bool`, `str`, `int`, `choice`.

## Хуки

Все хуки необязательны. Функции могут быть `async def` или обычными `def`.

```python
async def on_load(ctx):
    """Вызывается вручную вашим кодом после загрузки, если используется."""

async def on_message(ctx, chat, message):
    """Новое сообщение в Playerok-чате."""

async def on_deal(ctx, deal):
    """Новая исходящая сделка."""

async def on_command(ctx, command, args):
    """Расширение для командного роутера."""

async def on_schedule(ctx):
    """Точка расширения для периодических задач."""

async def on_unload(ctx):
    """Освобождение ресурсов перед перезагрузкой."""
```

На текущей версии worker автоматически вызывает `on_message` и `on_deal`. Остальные хуки доступны как API-контракт для расширений и будущих планировщиков.

## Контекст `ctx`

- `ctx.account` — активный аккаунт Playerok.
- `ctx.client` — клиент Playerok API.
- `ctx.bot` — экземпляр Telegram Bot.
- `ctx.db` — фабрика SQLAlchemy async-сессий.
- `ctx.config` — настройки текущего аккаунта.
- `await ctx.send_chat(chat_id, text)` — отправить сообщение в Playerok.
- `await ctx.notify(text)` — отправить уведомление владельцу в Telegram.
- `await ctx.get_item(item_id)` — получить полную информацию о лоте.
- `await ctx.update_item(item_id, name=..., price=..., description=..., add_attachments=..., remove_attachments=...)` — обновить название, цену, описание или фотографии лота.
- `ctx.external_api` — дополнительный HTTP-клиент, если он настроен.
- `ctx.telethon` — менеджер пользовательских Telegram-сессий, если он настроен.

## Пример работы с лотами (динамическая цена)

```python
async def on_schedule(ctx):
    # Обновить цену и название лота
    await ctx.update_item(
        item_id="123456",
        price=490,
        name="Супер лот (Скидка!)",
        description="Новое обновленное описание",
    )
    await ctx.notify("Цена лота 123456 обновлена до 490 ₽")
```

## Пример создания лота с выдачей (аккаунт, логин/пароль, фото)

```python
async def publish_game_account(ctx):
    # Создание лота с данными для автовыдачи покупателю
    new_item = await ctx.create_item(
        game_category_id="category_id_here",
        obtaining_type_id="obtaining_type_id_here",
        name="Личный аккаунт 80 LVL [Полный доступ]",
        price=1500,
        description="Продажа личного аккаунта. Почта перепривязана.",
        options={"server": "EU", "level": "80"},
        data_fields={
            "login_field_id": "account_login@mail.com",
            "password_field_id": "SecretPassword123",
        },
        attachments=["/path/to/screenshot1.png", b"...raw_image_bytes..."],
    )
    # Публикация лота на Playerok
    statuses = await ctx.client.get_item_priority_statuses(str(new_item.id), 1500)
    await ctx.client.publish_item(str(new_item.id), priority_status_id=str(statuses[0].id))
    await ctx.notify(f"Лот создан и выставлен: {new_item.name} (#{new_item.id})")
```

## Пример HTTP-запроса

```python
async def on_deal(ctx, deal):
    if not ctx.external_api:
        return
    response = await ctx.external_api.post(
        "https://example.com/webhook",
        json={"deal_id": str(getattr(deal, "id", ""))},
    )
    await ctx.notify(f"Webhook отправлен: {response.status_code}")
```

## Рекомендации для ИИ

Перед генерацией плагина передайте ИИ этот файл и опишите:

1. событие, на которое нужно реагировать;
2. условия срабатывания;
3. текст уведомлений и ответов;
4. нужные настройки;
5. внешний API, если он нужен.

Попросите ИИ вернуть один готовый файл `.py` без Markdown-обёртки, с `PLUGIN_META` и async-хуками.
