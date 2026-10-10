import html
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.accounts_manager import (
    UniversalAccountDeal,
    UniversalAccountLotRule,
    UniversalAccountStock,
    UniversalGame,
    format_universal_delivery_message,
)
from playerokapi.enums import ItemDealStatuses

logger = logging.getLogger("plugin.universal_accounts")

PLUGIN_META = {
    "id": "universal_accounts",
    "name": "Универсальная автовыдача аккаунтов",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Полностью настраиваемая автовыдача аккаунтов для любых игр и приложений: "
        "Black Russia, Brawl Stars, Standoff 2, Steam, Telegram, Roblox и др. "
        "Автоматически отправляет логин, пароль, ID аккаунта, сервер и инструкции покупателю, "
        "и завершает заказ на Playerok."
    ),
    "settings": {
        "auto_complete": {
            "type": "bool",
            "name": "Авто-завершение сделки (SENT)",
            "default": True,
            "description": "Автоматически отмечать сделку выполненной после выдачи аккаунта покупателю",
        },
        "notify_seller": {
            "type": "bool",
            "name": "Уведомлять продавца в Telegram",
            "default": True,
            "description": "Присылать уведомление в Telegram с данными выданного аккаунта и остатком склада",
        },
    },
}


async def on_load(ctx: Any) -> None:
    logger.info("Universal Accounts plugin loaded.")


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
        # Ищем активное правило привязки лота к игре
        rules = (await session.scalars(
            select(UniversalAccountLotRule).where(
                UniversalAccountLotRule.account_id == account.id,
                UniversalAccountLotRule.enabled.is_(True),
            )
        )).all()

        rule = None
        for r in rules:
            if str(r.lot_id).strip() == str(item_id).strip() or str(r.lot_id).strip() == "*":
                rule = r
                break

        if not rule and getattr(deal, "item", None):
            item_slug = str(getattr(deal.item, "slug", "") or "").strip()
            item_title = str(getattr(deal.item, "name", "") or "").strip()
            for r in rules:
                if (item_slug and r.lot_id == item_slug) or (item_title and r.lot_title.lower() == item_title.lower()):
                    rule = r
                    break

        if not rule:
            return

        # Проверяем, не выдавался ли уже аккаунт для этой сделки
        existing = await session.scalar(
            select(UniversalAccountDeal).where(
                UniversalAccountDeal.account_id == account.id,
                UniversalAccountDeal.deal_id == deal_id,
            )
        )
        if existing:
            return

        game = await session.get(UniversalGame, rule.game_id)
        game_title = game.title if game else "Игре"

        # Ищем свободный аккаунт на складе для этой игры
        stock = await session.scalar(
            select(UniversalAccountStock).where(
                UniversalAccountStock.account_id == account.id,
                UniversalAccountStock.game_id == rule.game_id,
                UniversalAccountStock.status == "available",
            ).order_by(UniversalAccountStock.id)
        )

        if not stock:
            # Склад пуст — уведомляем продавца
            await ctx.notify(
                f"⚠️ <b>{html.escape(game_title)}: Закончились аккаунты на складе!</b>\n\n"
                f"Покупатель оплатил лот <b>{html.escape(rule.lot_title)}</b>.\n"
                f"Сделка: <code>{html.escape(deal_id)}</code>\n\n"
                f"<i>Пожалуйста, срочно пополните склад аккаунтов {html.escape(game_title)} в боте!</i>"
            )
            return

        # Резервируем аккаунт со склада
        stock.status = "issued"
        stock.issued_deal_id = deal_id
        stock.issued_chat_id = chat_id
        stock.issued_at = datetime.now(timezone.utc)

        deal_record = UniversalAccountDeal(
            account_id=account.id,
            deal_id=deal_id,
            chat_id=chat_id,
            stock_id=stock.id,
            game_id=rule.game_id,
            login=stock.login,
            deal_completed=False,
        )
        session.add(deal_record)
        await session.commit()

        stock_login = stock.login
        stock_pwd_enc = stock.password_encrypted
        stock_acc_id = stock.account_identifier
        stock_server = stock.server
        stock_extra_enc = stock.additional_data_encrypted
        stock_comment = stock.comment

    # Дешифруем данные
    cipher = ctx.cipher
    if not cipher:
        from app.handlers import svc
        cipher = svc().cipher
    password = cipher.decrypt(stock_pwd_enc) if stock_pwd_enc else ""
    extra = cipher.decrypt(stock_extra_enc) if stock_extra_enc else ""

    # Формируем и отправляем сообщение покупателю в чат Playerok
    delivery_msg = format_universal_delivery_message(
        template=rule.delivery_template,
        game_title=game_title,
        lot_name=rule.lot_title,
        login=stock_login,
        password=password,
        account_id=stock_acc_id,
        server=stock_server,
        extra=extra,
        comment=stock_comment,
    )
    await ctx.send_chat(chat_id, delivery_msg)

    # Автозавершение сделки (перевод в SENT / выполнен)
    auto_complete = rule.auto_sent if rule.auto_sent is not None else bool(ctx.config.get("auto_complete", True))
    if auto_complete:
        try:
            await ctx.update_deal(deal_id, ItemDealStatuses.SENT)
            async with ctx.db() as session:
                rec = await session.scalar(
                    select(UniversalAccountDeal).where(
                        UniversalAccountDeal.account_id == account.id,
                        UniversalAccountDeal.deal_id == deal_id,
                    )
                )
                if rec:
                    rec.deal_completed = True
                    await session.commit()
        except Exception as exc:
            logger.warning("Не удалось перевести сделку %s в SENT: %s", deal_id, exc)

    # Уведомляем продавца в Telegram
    notify_seller = bool(ctx.config.get("notify_seller", True))
    if notify_seller:
        # Подсчитываем остаток аккаунтов на складе
        async with ctx.db() as session:
            from sqlalchemy import func
            rem_count = await session.scalar(
                select(func.count(UniversalAccountStock.id)).where(
                    UniversalAccountStock.account_id == account.id,
                    UniversalAccountStock.game_id == rule.game_id,
                    UniversalAccountStock.status == "available",
                )
            ) or 0

        extra_line = f"\n🌐 Сервер: <code>{html.escape(stock_server)}</code>" if stock_server else ""
        id_line = f"\n🆔 ID/Ник: <code>{html.escape(stock_acc_id)}</code>" if stock_acc_id else ""
        await ctx.notify(
            f"🎮 <b>Выдан аккаунт {html.escape(game_title)}</b>\n\n"
            f"🆔 Сделка: <code>{html.escape(deal_id)}</code>\n"
            f"📦 Товар: <b>{html.escape(rule.lot_title)}</b>\n"
            f"🔑 Логин: <code>{html.escape(stock_login)}</code>\n"
            f"🔒 Пароль: <code>{html.escape(password)}</code>"
            f"{id_line}{extra_line}\n\n"
            f"📊 Остаток на складе: <b>{rem_count} шт.</b>\n"
            "✅ Данные успешно переданы покупателю в чат Playerok!"
        )
