from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.supercell_manager import (
    SUPERCELL_GAMES,
    SupercellDeal,
    SupercellLotRule,
    SupercellStock,
    fetch_supercell_auth_code,
    format_supercell_delivery_message,
)
from playerokapi.enums import ItemDealStatuses

logger = logging.getLogger("plugin.supercell_seller")

PLUGIN_META = {
    "id": "supercell_seller",
    "name": "Supercell ID Автовыдача",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Специализированная автовыдача аккаунтов Supercell ID: "
        "Brawl Stars, Clash Royale, Clash of Clans, Squad Busters, Hay Day. "
        "Выдает привязанную почту, пароль, трофеи, уровень ТХ, количество бравлеров, "
        "коды восстановления Supercell ID и пошаговую инструкцию по входу в игру."
    ),
    "settings": {
        "auto_complete": {
            "type": "bool",
            "name": "Автозавершение сделки (SENT)",
            "default": True,
            "description": "Автоматически отмечать сделку выполненной после отправки аккаунта покупателю",
        },
        "notify_seller": {
            "type": "bool",
            "name": "Уведомлять продавца в Telegram",
            "default": True,
            "description": "Присылать отчет об успешной выдаче аккаунта и остатке на складе в Telegram",
        },
    },
}


async def on_load(ctx: Any) -> None:
    logger.info("Supercell Seller plugin loaded.")


async def on_deal(ctx: Any, deal: Any) -> None:
    """Вызывается при появлении нового оплаченного заказа."""
    account = ctx.account
    item = getattr(deal, "item", None)
    item_id = str(getattr(item, "id", "") or "")
    deal_id = str(getattr(deal, "id", "") or "")
    chat = getattr(deal, "chat", None)
    chat_id = str(getattr(chat, "id", "") or "")

    # Если chat_id или item_id не загружены в объекте сделки, подтягиваем через API
    if (not chat_id or not item_id) and deal_id:
        try:
            full_deal = await ctx.get_deal(deal_id)
            if full_deal:
                if not item_id and getattr(full_deal, "item", None):
                    item = full_deal.item
                    item_id = str(getattr(item, "id", "") or "")
                if not chat_id and getattr(full_deal, "chat", None):
                    chat = full_deal.chat
                    chat_id = str(getattr(chat, "id", "") or "")
        except Exception as exc:
            logger.debug("on_deal get_deal fallback error: %s", exc)

    if not item_id or not deal_id or not chat_id:
        return

    async with ctx.db() as session:
        # Ищем активное правило привязки лота к Supercell
        rules = (await session.scalars(
            select(SupercellLotRule).where(
                SupercellLotRule.account_id == account.id,
                SupercellLotRule.enabled.is_(True),
            )
        )).all()

        rule = None
        for r in rules:
            if str(r.lot_id).strip() == str(item_id).strip() or str(r.lot_id).strip() == "*":
                rule = r
                break

        # Если не нашли по прямому ID, проверяем slug или название лота
        if not rule and getattr(deal, "item", None):
            item_slug = str(getattr(deal.item, "slug", "") or "").strip()
            item_title = str(getattr(deal.item, "name", "") or "").strip().lower()
            for r in rules:
                if (item_slug and r.lot_id == item_slug) or (item_title and r.lot_title.strip().lower() in item_title):
                    rule = r
                    break

        if not rule:
            return

        # Проверяем, не выдавался ли уже аккаунт для этой сделки
        existing = await session.scalar(
            select(SupercellDeal).where(
                SupercellDeal.account_id == account.id,
                SupercellDeal.deal_id == deal_id,
            )
        )
        if existing:
            return

        # Ищем свободный аккаунт на складе для выбранной игры
        stock = await session.scalar(
            select(SupercellStock).where(
                SupercellStock.account_id == account.id,
                SupercellStock.game_key == rule.game_key,
                SupercellStock.status == "available",
            ).order_by(SupercellStock.id.asc())
        )

        game_name = SUPERCELL_GAMES.get(rule.game_key, rule.game_key)

        if not stock:
            # Склад пуст — уведомляем продавца
            logger.warning(
                "Supercell stock empty for game '%s' on account %s (deal %s)",
                rule.game_key, account.id, deal_id,
            )
            msg = (
                f"⚠️ <b>Внимание: склад Supercell ID пуст!</b>\n\n"
                f"Игра: <b>{html.escape(game_name)}</b>\n"
                f"Товар: <b>{html.escape(rule.lot_title)}</b>\n"
                f"Сделка: <code>{deal_id}</code>\n\n"
                f"Покупатель оплатил заказ, но свободных аккаунтов на складе нет. "
                f"Пожалуйста, выдайте аккаунт вручную в чате с покупателем."
            )
            await ctx.notify(msg)
            return

        # Расшифровываем пароль и коды восстановления
        cipher = getattr(ctx, "cipher", None)
        if cipher:
            try:
                password = cipher.decrypt(stock.password_encrypted)
            except Exception:
                password = stock.password_encrypted
            if stock.recovery_codes_encrypted:
                try:
                    recovery_codes = cipher.decrypt(stock.recovery_codes_encrypted)
                except Exception:
                    recovery_codes = stock.recovery_codes_encrypted
            else:
                recovery_codes = None
        else:
            password = stock.password_encrypted
            recovery_codes = stock.recovery_codes_encrypted

        lot_name = getattr(item, "name", None) or rule.lot_title

        # Формируем итоговое сообщение покупателю
        delivery_text = format_supercell_delivery_message(
            template=rule.delivery_template,
            game_key=rule.game_key,
            lot_name=lot_name,
            email=stock.email,
            password=password,
            trophies=stock.trophies,
            town_hall=stock.town_hall,
            brawlers_count=stock.brawlers_count,
            recovery_codes=recovery_codes,
            has_mail_access=stock.has_mail_access,
            comment=stock.comment,
        )

        # Отправляем в чат сделки на Playerok
        try:
            await ctx.send_chat(chat_id, delivery_text)
            logger.info("Sent Supercell account for deal %s in chat %s", deal_id, chat_id)
        except Exception as exc:
            logger.error("Failed to send Supercell account in chat %s: %s", chat_id, exc)
            await ctx.notify(
                f"❌ <b>Ошибка отправки аккаунта Supercell:</b>\n"
                f"Сделка: <code>{deal_id}</code>\n"
                f"Ошибка: {html.escape(str(exc))}"
            )
            return

        # Помечаем аккаунт как выданный
        now = datetime.now(timezone.utc)
        stock.status = "issued"
        stock.issued_deal_id = deal_id
        stock.issued_chat_id = chat_id
        stock.issued_at = now

        # Записываем сделку
        sup_deal = SupercellDeal(
            account_id=account.id,
            deal_id=deal_id,
            chat_id=chat_id,
            stock_id=stock.id,
            game_key=rule.game_key,
            email=stock.email,
            deal_completed=False,
        )
        session.add(sup_deal)
        await session.commit()

        # Автозавершение сделки (SENT), если включено в правиле и в настройках
        cfg = getattr(ctx, "config", {}) or {}
        auto_complete = cfg.get("auto_complete", True) and rule.auto_sent
        if auto_complete:
            try:
                # Даем паузу перед подтверждением
                await ctx.client.set_deal_status(deal_id, ItemDealStatuses.SENT)
                sup_deal.deal_completed = True
                await session.commit()
                logger.info("Auto-completed Supercell deal %s with status SENT", deal_id)
            except Exception as exc:
                logger.warning("Could not auto-complete deal %s: %s", deal_id, exc)

        # Уведомляем продавца в Telegram
        notify_seller = cfg.get("notify_seller", True)
        if notify_seller:
            remaining = (await session.scalar(
                select(func.count(SupercellStock.id)).where(
                    SupercellStock.account_id == account.id,
                    SupercellStock.game_key == rule.game_key,
                    SupercellStock.status == "available",
                )
            )) or 0

            trophies_str = f" | 🏆 {stock.trophies:,}" if stock.trophies is not None else ""
            await ctx.notify(
                f"⚡ <b>Успешная автовыдача Supercell ID!</b>\n\n"
                f"🎮 Игра: <b>{html.escape(game_name)}</b>\n"
                f"📦 Лот: <b>{html.escape(lot_name)}</b>\n"
                f"🆔 Сделка: <code>{deal_id}</code>\n\n"
                f"📧 Почта: <code>{html.escape(stock.email)}</code>{trophies_str}\n"
                f"📊 Остаток на складе [{game_name}]: <b>{remaining} шт.</b>\n"
                f"Статус сделки: <b>{'Завершена (SENT)' if sup_deal.deal_completed else 'Ожидает'}</b>"
            )


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    """Обрабатывает запросы покупателя в чате с командой получения кода с почты (#код / #code)."""
    text = str(getattr(message, "text", "") or "").strip().lower()
    if not text:
        return

    # Распознаем триггеры получения кода Supercell ID
    is_code_request = any(k in text for k in ("#код", "#code", "код", "code", "дай код", "скинь код"))
    is_help_request = text in {"#supercell", "#help", "#помощь"}

    if not (is_code_request or is_help_request):
        return

    chat_id = str(getattr(chat, "id", "") or "")
    if not chat_id:
        return

    async with ctx.db() as session:
        # Ищем последнюю сделку в этом чате
        deal_record = await session.scalar(
            select(SupercellDeal).where(
                SupercellDeal.account_id == ctx.account.id,
                SupercellDeal.chat_id == chat_id,
            ).order_by(SupercellDeal.id.desc())
        )
        if not deal_record:
            return

        stock = await session.get(SupercellStock, deal_record.stock_id)
        if not stock:
            return

        game_name = SUPERCELL_GAMES.get(deal_record.game_key, "Supercell ID")

        cipher = getattr(ctx, "cipher", None)
        try:
            password = cipher.decrypt(stock.password_encrypted) if cipher else stock.password_encrypted
        except Exception:
            password = stock.password_encrypted

        # Если запрошен код подтверждения
        if is_code_request:
            # 1. Пытаемся автоматически зайти на почту и прочесть письмо
            try:
                auth_code = await fetch_supercell_auth_code(stock.email, password)
            except Exception as exc:
                logger.debug("fetch_supercell_auth_code error: %s", exc)
                auth_code = None

            if auth_code:
                reply = (
                    f"🔢 <b>Ваш код подтверждения {html.escape(game_name)}:</b>\n\n"
                    f"👉 <code>{auth_code}</code> 👈\n\n"
                    f"📧 Почта: <code>{html.escape(stock.email)}</code>\n"
                    f"<i>Введите этот 6-значный код в игре для завершения входа.</i>"
                )
                try:
                    await ctx.send_chat(chat_id, reply)
                    logger.info("Sent Supercell OTP code %s to buyer in chat %s", auth_code, chat_id)
                except Exception as exc:
                    logger.debug("Failed to send OTP code to chat: %s", exc)
                return
            else:
                # Если письмо ещё не дошло или задержка со стороны Supercell
                reply = (
                    f"⏳ <b>Код подтверждения пока не поступил на почту</b> <code>{html.escape(stock.email)}</code>.\n\n"
                    f"1. Убедитесь, что вы нажали «Войти» в игре и отправили запрос на этот email.\n"
                    f"2. Подождите 15-30 секунд и отправьте <code>#код</code> ещё раз.\n"
                    f"3. Вы также можете самостоятельно зайти на почту с выданным паролем."
                )
                try:
                    await ctx.send_chat(chat_id, reply)
                except Exception as exc:
                    logger.debug("Failed to send waiting reply: %s", exc)
                return

        # Памятка помощи #help / #supercell
        rec_codes = None
        if stock.recovery_codes_encrypted and cipher:
            try:
                rec_codes = cipher.decrypt(stock.recovery_codes_encrypted)
            except Exception:
                rec_codes = stock.recovery_codes_encrypted
        elif stock.recovery_codes_encrypted:
            rec_codes = stock.recovery_codes_encrypted

        extra_msg = ""
        if rec_codes:
            extra_msg = (
                f"\n🔐 <b>Коды восстановления Supercell ID:</b>\n"
                f"<code>{html.escape(rec_codes)}</code>\n"
            )

        reply = (
            f"ℹ️ <b>Памятка по входу в Supercell ID ({html.escape(game_name)}):</b>\n\n"
            f"📧 Почта: <code>{html.escape(stock.email)}</code>\n"
            f"🔑 Пароль: <code>{html.escape(password)}</code>\n"
            f"{extra_msg}\n"
            f"💡 <b>Чтобы получить свежий код из письма</b>, просто отправьте в этот чат команду <code>#код</code>!"
        )
        try:
            await ctx.send_chat(chat_id, reply)
        except Exception as exc:
            logger.debug("Failed to send supercell help message: %s", exc)
