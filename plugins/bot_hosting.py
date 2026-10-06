from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

from sqlalchemy import select

from app.handlers import svc
from app.bot_store_manager import (
    DEFAULT_API_URL,
    GITHUB_SOURCES_URL,
    BotStoreConfig,
    BotStoreDeal,
    BotStoreLotRule,
    send_to_hosting_api,
)
from playerokapi.enums import ItemDealStatuses

logger = logging.getLogger("plugin.bot_hosting")

PLUGIN_META = {
    "id": "bot_hosting",
    "name": "Bot Hosting & Delivery",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Автовыдача сурсов Telegram-ботов (Магазин + Mini App) и автоматический хостинг через наш REST API. "
        "После оплаты выдает сурсы и спрашивает #да или #нет. "
        "При #да запрашивает BOT_TOKEN и ADMIN_ID, деплоит на хостинг и переводит сделку в «Выполнен»."
    ),
    "settings": {},
}


async def on_deal(ctx: Any, deal: Any) -> None:
    """Вызывается при появлении нового оплаченного заказа."""
    account = ctx.account
    item = getattr(deal, "item", None)
    item_id = str(getattr(item, "id", "") or "")
    if not item_id:
        return

    deal_id = str(getattr(deal, "id", "") or "")
    chat = getattr(deal, "chat", None)
    chat_id = str(getattr(chat, "id", "") or "")
    if not deal_id or not chat_id:
        return

    async with ctx.db() as session:
        # Проверяем, привязан ли лот к правилам плагина
        rule = await session.scalar(
            select(BotStoreLotRule).where(
                BotStoreLotRule.account_id == account.id,
                BotStoreLotRule.lot_id == item_id,
                BotStoreLotRule.enabled.is_(True),
            )
        )
        if not rule:
            return

        # Проверяем, не обрабатывалась ли уже сделка
        existing = await session.scalar(
            select(BotStoreDeal).where(
                BotStoreDeal.account_id == account.id,
                BotStoreDeal.deal_id == deal_id,
            )
        )
        if existing:
            return

        # Создаем запись о сделке
        new_deal = BotStoreDeal(
            account_id=account.id,
            deal_id=deal_id,
            chat_id=chat_id,
            lot_id=item_id,
            bot_type=rule.bot_type,
            status="waiting_choice",
        )
        session.add(new_deal)
        await session.commit()

    # Отправляем сурсы и вопрос о хостинге покупателю
    msg_sources = (
        "Здравствуйте! Спасибо за покупку 📦\n\n"
        f"Ваши исходные коды проекта (Бот-Магазин + Web Mini App):\n"
        f"🔗 {GITHUB_SOURCES_URL}\n\n"
        "────────────────────────────\n"
        "Хотите, чтобы мы автоматически запустили (захостили) вашего бота прямо сейчас?\n\n"
        "Отправьте в чат:\n"
        "👉 <b>#да</b> — если нужно запустить бота на хостинге.\n"
        "👉 <b>#нет</b> — если вы забираете только исходники."
    )
    await ctx.send_chat(chat_id, msg_sources)
    await ctx.notify(
        f"📦 <b>Bot Hosting: Выданы сурсы по сделке #{html.escape(deal_id)}</b>\n"
        f"Лот: <code>{html.escape(rule.lot_title)}</code>\n"
        "Ожидаем ответ покупателя (#да / #нет)..."
    )


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    """Обработка сообщений в Playerok чате (#да, #нет, токен, admin_id)."""
    text = str(getattr(message, "text", "") or "").strip()
    if not text:
        return

    # Игнорируем собственные сообщения бота
    is_from_me = bool(getattr(message, "is_from_me", False))
    if is_from_me:
        return

    chat_id = str(getattr(chat, "id", "") or "")
    if not chat_id:
        return

    account = ctx.account

    async with ctx.db() as session:
        # Ищем активную незавершенную сделку в этом чате
        deal_entry = await session.scalar(
            select(BotStoreDeal).where(
                BotStoreDeal.account_id == account.id,
                BotStoreDeal.chat_id == chat_id,
                BotStoreDeal.status.in_(["waiting_choice", "waiting_token", "waiting_admin_id"]),
            ).order_by(BotStoreDeal.id.desc())
        )
        if not deal_entry:
            return

        cfg = await session.scalar(
            select(BotStoreConfig).where(BotStoreConfig.account_id == account.id)
        )
        api_url = (cfg.api_url if cfg and cfg.api_url else DEFAULT_API_URL).rstrip("/")
        api_key = svc().cipher.decrypt(cfg.api_key_enc) if (cfg and cfg.api_key_enc) else ""

        # ЭТАП 1: Ожидание выбора (#да / #нет)
        if deal_entry.status == "waiting_choice":
            lower = text.lower()
            if "#нет" in lower or lower == "нет":
                deal_entry.status = "completed"
                await session.commit()

                # Подтверждаем выполнение заказа в Playerok
                await complete_deal_in_playerok(ctx, deal_entry.deal_id)

                await ctx.send_chat(
                    chat_id,
                    "Отлично! Сурсы остаются у вас. Заказ переведен в статус выполненного.\n"
                    "Пожалуйста, подтвердите получение и оставьте положительный отзыв! ⭐⭐⭐⭐⭐"
                )
                await ctx.notify(
                    f"✅ <b>Bot Hosting: Сделка #{deal_entry.deal_id} завершена (#нет)</b>\n"
                    "Покупатель выбрал только исходники."
                )
                return

            if "#да" in lower or lower == "да":
                deal_entry.status = "waiting_token"
                await session.commit()

                await ctx.send_chat(
                    chat_id,
                    "Отлично! Начинаем настройку хостинга для вашего бота 🚀\n\n"
                    "1️⃣ <b>Шаг 1 из 2:</b> Отправьте <b>BOT_TOKEN</b> вашего Telegram-бота.\n\n"
                    "ℹ️ <i>Как получить токен:</i>\n"
                    "• Откройте в Telegram официального бота @BotFather\n"
                    "• Отправьте команду /newbot и задайте имя и username\n"
                    "• Скопируйте полученный API токен вида <code>123456789:AA...</code> и отправьте его сюда."
                )
                return

        # ЭТАП 2: Получение токена бота
        if deal_entry.status == "waiting_token":
            # Валидация формата токена
            match = re.search(r'\d{5,}:[A-Za-z0-9_-]{20,}', text)
            if not match:
                await ctx.send_chat(
                    chat_id,
                    "❌ Неверный формат токена бота!\n"
                    "Токен должен иметь вид: <code>123456789:AA...</code> от @BotFather.\n"
                    "Пожалуйста, скопируйте и отправьте правильный токен."
                )
                return

            bot_token = match.group(0).strip()
            deal_entry.bot_token = bot_token
            deal_entry.status = "waiting_admin_id"
            await session.commit()

            await ctx.send_chat(
                chat_id,
                "Токен принят! ✅\n\n"
                "2️⃣ <b>Шаг 2 из 2:</b> Отправьте ваш <b>ADMIN_ID</b> (числовой ID в Telegram).\n\n"
                "ℹ️ <i>Как узнать свой ID:</i>\n"
                "• Напишите боту @userinfobot или @myidbot в Telegram\n"
                "• Он пришлет ваш цифровой ID (например: <code>123456789</code>).\n"
                "Отправьте этот номер сюда сообщением."
            )
            return

        # ЭТАП 3: Получение ADMIN_ID и запуск на хостинге
        if deal_entry.status == "waiting_admin_id":
            # Ищем число
            match = re.search(r'\b\d{5,15}\b', text)
            if not match:
                await ctx.send_chat(
                    chat_id,
                    "❌ Неверный формат ID администратора!\n"
                    "Укажите числовой ID от @userinfobot (только цифры, например: <code>123456789</code>)."
                )
                return

            admin_id = match.group(0).strip()
            deal_entry.admin_id = admin_id
            deal_entry.status = "hosting"
            await session.commit()

            if not api_key:
                deal_entry.status = "waiting_admin_id"
                await session.commit()
                await ctx.send_chat(
                    chat_id,
                    "⚠️ Ошибка сервера: у продавца не настроен API-ключ хостинга. "
                    "Продавец уже уведомлен и запустит вашего бота вручную."
                )
                await ctx.notify(
                    f"🚨 <b>Внимание! Не настроен API Key в Bot Hosting!</b>\n"
                    f"Сделка #{deal_entry.deal_id}. Токен: <code>{deal_entry.bot_token}</code>, Admin ID: <code>{admin_id}</code>.\n"
                    "Укажите API Key в панели плагина!"
                )
                return

            await ctx.send_chat(
                chat_id,
                "⏳ Запускаем и настраиваем вашего бота на сервере, пожалуйста, подождите пару секунд..."
            )

            # Отправка в наш Public API
            try:
                bot_name = f"store-deal-{deal_entry.deal_id}"
                result = await send_to_hosting_api(
                    api_url=api_url,
                    api_key=api_key,
                    name=bot_name,
                    token=deal_entry.bot_token,
                    admin_id=admin_id,
                )
                hosted_id = result.get("id", "ok")
                deal_entry.hosted_bot_id = str(hosted_id)
                deal_entry.status = "completed"
                await session.commit()

                # Отмечаем выполнение заказа
                await complete_deal_in_playerok(ctx, deal_entry.deal_id)

                await ctx.send_chat(
                    chat_id,
                    "🎉 <b>Ваш бот успешно захощен и запущен!</b>\n\n"
                    "✨ Все процессы активны на сервере.\n"
                    "Заказ отмечен как выполненный продавцом.\n"
                    "Пожалуйста, проверьте работу бота, подтвердите получение заказа и оставьте отзыв 5★! Спасибо!"
                )

                await ctx.notify(
                    f"🚀 <b>Бот успешно захощен!</b>\n\n"
                    f"Сделка: <code>#{deal_entry.deal_id}</code>\n"
                    f"Bot ID в системе: <code>{hosted_id}</code>\n"
                    f"Admin ID покупателя: <code>{admin_id}</code>\n"
                    "Заказ автоматически подтвержден как выполненный!"
                )

            except Exception as exc:
                logger.error("Failed to host bot via API: %s", exc)
                deal_entry.status = "waiting_admin_id"  # даем возможность повторить
                await session.commit()

                await ctx.send_chat(
                    chat_id,
                    "⚠️ Возникла временная задержка при автохостинге. "
                    "Продавец получил ваши данные и проверит деплой в ближайшее время."
                )
                await ctx.notify(
                    f"❌ <b>Ошибка вызова Hosting API!</b>\n"
                    f"Сделка #{deal_entry.deal_id}\n"
                    f"Ошибка: <code>{html.escape(str(exc))}</code>\n"
                    f"Token: <code>{deal_entry.bot_token}</code>\n"
                    f"Admin ID: <code>{admin_id}</code>"
                )


async def complete_deal_in_playerok(ctx: Any, deal_id: str) -> None:
    """Подтверждение выполнения сделки продавцом."""
    try:
        await ctx.gateway.set_deal_status(ctx.account, deal_id, ItemDealStatuses.SENT)
    except Exception as e:
        logger.warning("Could not set deal status to SENT: %s", e)
