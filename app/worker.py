from __future__ import annotations

import asyncio
import html
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from playerokapi.enums import ChatTypes, ItemDealDirections, ItemDealStatuses

from .crypto import SecretCipher
from .models import AutoReplyRule, DeliveryRule, DeliveryStock, PlayerokAccount, ProcessedEvent
from .playerok import PlayerokGateway
from .plugin_system import PluginManager


logger = logging.getLogger(__name__)


def settings_for(account: PlayerokAccount) -> dict[str, Any]:
    raw = account.settings or {}
    notifications = {
        "new_message": True,
        "new_deal": True,
        "deal_status": True,
        "errors": True,
    }
    notifications.update(raw.get("notifications") or {})
    return {
        "notifications": notifications,
        "auto_confirm": bool(raw.get("auto_confirm", False)),
    }


async def claim_event(
    factory: async_sessionmaker[AsyncSession],
    account_id: Any,
    kind: str,
    external_id: str,
) -> bool:
    async with factory() as session:
        dialect_name = session.bind.dialect.name if session.bind else ""
        if dialect_name == "postgresql":
            stmt = (
                pg_insert(ProcessedEvent)
                .values(account_id=account_id, kind=kind, external_id=str(external_id))
                .on_conflict_do_nothing(index_elements=["account_id", "kind", "external_id"])
            )
        else:
            stmt = (
                sqlite_insert(ProcessedEvent)
                .values(account_id=account_id, kind=kind, external_id=str(external_id))
                .on_conflict_do_nothing(index_elements=["account_id", "kind", "external_id"])
            )
        result = await session.execute(stmt)
        await session.commit()
        return bool(result.rowcount)


async def release_event(
    factory: async_sessionmaker[AsyncSession],
    account_id: Any,
    kind: str,
    external_id: str,
) -> None:
    async with factory() as session:
        await session.execute(
            delete(ProcessedEvent).where(
                ProcessedEvent.account_id == account_id,
                ProcessedEvent.kind == kind,
                ProcessedEvent.external_id == str(external_id),
            )
        )
        await session.commit()


class WorkerManager:
    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: PlayerokGateway,
        cipher: SecretCipher,
        poll_interval: float,
        plugin_manager: PluginManager | None = None,
    ):
        self.bot = bot
        self.db = session_factory
        self.gateway = gateway
        self.cipher = cipher
        self.poll_interval = poll_interval
        self.plugins = plugin_manager
        self._stop = asyncio.Event()
        self._sem = asyncio.Semaphore(4)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("Playerok worker started")
        while not self._stop.is_set():
            try:
                async with self.db() as session:
                    accounts = list((await session.scalars(select(PlayerokAccount))).all())
                await asyncio.gather(*(self._guarded(account) for account in accounts))
            except Exception:
                logger.exception("Worker cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _guarded(self, account: PlayerokAccount) -> None:
        async with self._sem:
            try:
                await self._poll(account)
            except Exception as exc:
                logger.exception("Playerok poll failed for %s", account.username)
                if settings_for(account)["notifications"]["errors"]:
                    key = f"{type(exc).__name__}:{datetime.now(timezone.utc):%Y%m%d%H}"
                    if await claim_event(self.db, account.id, "error", key):
                        await self._notify(
                            account.tg_user_id,
                            "⚠️ <b>Playerok ошибка</b>\n"
                            f"Аккаунт: <b>{html.escape(account.username)}</b>\n"
                            f"{html.escape(str(exc))[:1200]}",
                        )

    async def _poll(self, account: PlayerokAccount) -> None:
        client = await self.gateway.get_client(account)
        # Fetch deals (outgoing sales and incoming purchases) + chats (PM, Notifications, Support)
        deals_out_page, deals_in_page, chats_page = await asyncio.gather(
            client.get_deals(direction=ItemDealDirections.OUT, count=24),
            client.get_deals(direction=ItemDealDirections.IN, count=24),
            client.get_chats(count=24),
        )
        deals = list(getattr(deals_out_page, "deals", []) or []) + list(getattr(deals_in_page, "deals", []) or [])
        chats = list(getattr(chats_page, "chats", []) or [])

        # Deduplicate deals by ID if any overlap
        seen_deals: set[str] = set()
        unique_deals = []
        for d in deals:
            did = str(getattr(d, "id", ""))
            if did and did not in seen_deals:
                seen_deals.add(did)
                unique_deals.append(d)
        deals = unique_deals

        if not account.worker_initialized:
            for deal in deals:
                status = getattr(getattr(deal, "status", None), "name", "UNKNOWN")
                await claim_event(self.db, account.id, "deal", deal.id)
                await claim_event(self.db, account.id, "deal_status", f"{deal.id}:{status}")
                await claim_event(self.db, account.id, "delivery_action", deal.id)
                await claim_event(self.db, account.id, "autoconfirm_action", deal.id)
            for chat in chats:
                message = getattr(chat, "last_message", None)
                if message:
                    await claim_event(self.db, account.id, "message", message.id)
            async with self.db() as session:
                row = await session.get(PlayerokAccount, account.id)
                if row:
                    row.worker_initialized = True
                    await session.commit()
            return

        for deal in reversed(deals):
            await self._deal(account, client, deal)
        for chat in reversed(chats):
            await self._chat(account, client, chat)

    async def _deal(self, account: PlayerokAccount, client: Any, deal: Any) -> None:
        cfg = settings_for(account)
        status = getattr(deal, "status", None)
        status_name = getattr(status, "name", "UNKNOWN")
        new_deal = await claim_event(self.db, account.id, "deal", deal.id)
        new_status = await claim_event(
            self.db, account.id, "deal_status", f"{deal.id}:{status_name}"
        )

        item = getattr(deal, "item", None)
        buyer = getattr(deal, "user", None)
        item_name = getattr(item, "name", None) or "Товар"
        item_id = str(getattr(item, "id", "") or "")
        buyer_name = getattr(buyer, "username", None) or "покупатель"
        price = getattr(item, "price", None)

        if new_deal and self.plugins:
            await self.plugins.dispatch_deal(account, client, self.bot, deal)

        if new_deal and cfg["notifications"]["new_deal"]:
            amount = f"\nСумма: <b>{html.escape(str(price))} ₽</b>" if price is not None else ""
            await self._notify(
                account.tg_user_id,
                "🛒 <b>Новый заказ</b>\n"
                f"Аккаунт: <b>{html.escape(account.username)}</b>\n"
                f"Товар: {html.escape(str(item_name))}\n"
                f"Покупатель: {html.escape(str(buyer_name))}{amount}\n"
                f"Deal ID: <code>{html.escape(str(deal.id))}</code>",
            )
        if new_status and not new_deal and cfg["notifications"]["deal_status"]:
            await self._notify(
                account.tg_user_id,
                "🔄 <b>Статус заказа изменён</b>\n"
                f"{html.escape(str(item_name))}\n"
                f"Статус: <code>{html.escape(status_name)}</code>\n"
                f"Deal ID: <code>{html.escape(str(deal.id))}</code>",
            )

        if status not in {ItemDealStatuses.PAID, ItemDealStatuses.PENDING}:
            return

        delivery = await self._delivery(account, client, deal, item_id)
        if cfg["auto_confirm"] and delivery is not False:
            action_id = str(deal.id)
            if await claim_event(self.db, account.id, "autoconfirm_action", action_id):
                try:
                    await client.update_deal(action_id, ItemDealStatuses.SENT)
                except Exception:
                    await release_event(self.db, account.id, "autoconfirm_action", action_id)
                    raise
                if cfg["notifications"]["deal_status"]:
                    await self._notify(
                        account.tg_user_id,
                        f"✅ Заказ <code>{html.escape(action_id)}</code> автоматически отмечен выполненным.",
                    )

    async def _delivery(
        self, account: PlayerokAccount, client: Any, deal: Any, item_id: str
    ) -> bool | None:
        async with self.db() as session:
            rule = await session.scalar(
                select(DeliveryRule).where(
                    DeliveryRule.account_id == account.id,
                    DeliveryRule.enabled.is_(True),
                    DeliveryRule.item_id == item_id,
                )
            )
            if not rule:
                rule = await session.scalar(
                    select(DeliveryRule).where(
                        DeliveryRule.account_id == account.id,
                        DeliveryRule.enabled.is_(True),
                        DeliveryRule.item_id == "*",
                    )
                )
        if not rule:
            return None

        action_id = str(deal.id)
        if not await claim_event(self.db, account.id, "delivery_action", action_id):
            return True

        chat_id = getattr(getattr(deal, "chat", None), "id", None)
        if not chat_id:
            await release_event(self.db, account.id, "delivery_action", action_id)
            return False

        buyer = getattr(getattr(deal, "user", None), "username", "") or ""
        item_name = getattr(getattr(deal, "item", None), "name", "") or ""
        values = defaultdict(str, {"buyer": buyer, "item": item_name, "deal_id": action_id})
        stock_id: int | None = None

        if rule.mode == "stock":
            async with self.db() as session:
                stock = await session.scalar(
                    select(DeliveryStock)
                    .where(DeliveryStock.rule_id == rule.id, DeliveryStock.used_at.is_(None))
                    .order_by(DeliveryStock.id)
                )
                if not stock:
                    await release_event(self.db, account.id, "delivery_action", action_id)
                    warn = f"{action_id}:{rule.id}:{datetime.now(timezone.utc):%Y%m%d%H}"
                    if await claim_event(self.db, account.id, "stock_empty", warn):
                        await self._notify(
                            account.tg_user_id,
                            "⚠️ <b>Автовыдача: склад пуст</b>\n"
                            f"Товар: {html.escape(item_name)}\n"
                            f"Deal ID: <code>{html.escape(action_id)}</code>",
                        )
                    return False
                stock_id = stock.id
                values["stock"] = self.cipher.decrypt(stock.payload_encrypted) or ""
                stock.used_at = datetime.now(timezone.utc)
                stock.deal_id = action_id
                await session.commit()

        try:
            await client.send_message(
                str(chat_id), rule.message_template.format_map(values), mark_chat_as_read=True
            )
            await self._notify(
                account.tg_user_id,
                f"⚡ Автовыдача отправлена по заказу <code>{html.escape(action_id)}</code>.",
            )
            return True
        except Exception:
            await release_event(self.db, account.id, "delivery_action", action_id)
            if stock_id is not None:
                async with self.db() as session:
                    stock = await session.get(DeliveryStock, stock_id)
                    if stock and stock.deal_id == action_id:
                        stock.used_at = None
                        stock.deal_id = None
                        await session.commit()
            raise

    async def _chat(self, account: PlayerokAccount, client: Any, chat: Any) -> None:
        message = getattr(chat, "last_message", None)
        if not message:
            return
        sender = getattr(message, "user", None)
        sender_id = str(getattr(sender, "id", "") or "")
        # Don't notify about our own outgoing messages
        if sender_id and sender_id == str(account.playerok_user_id):
            return
        if getattr(message, "is_auto_response", False):
            return
        if not await claim_event(self.db, account.id, "message", message.id):
            return

        text = str(getattr(message, "text", "") or "")
        images = list(getattr(message, "images", []) or [])
        images_info = f"\n📷 <i>Прикреплено фото: {len(images)} шт.</i>" if images else ""

        chat_type_obj = getattr(chat, "type", None)
        chat_type_name = getattr(chat_type_obj, "name", "")
        type_badge = ""
        if chat_type_name == "NOTIFICATIONS":
            type_badge = " [Уведомление]"
        elif chat_type_name == "SUPPORT":
            type_badge = " [Поддержка]"

        sender_name = getattr(sender, "username", None)
        if not sender_name:
            if chat_type_name == "NOTIFICATIONS":
                sender_name = "Playerok Уведомления"
            elif chat_type_name == "SUPPORT":
                sender_name = "Служба поддержки Playerok"
            else:
                sender_name = "Пользователь"

        display_text = text
        if not display_text and not images:
            event = getattr(getattr(message, "event", None), "name", None)
            display_text = f"[{event or 'Системное событие'}]"

        if settings_for(account)["notifications"]["new_message"]:
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="💬 Открыть чат", callback_data=f"chat:open:{chat.id}"),
                    InlineKeyboardButton(text="✍️ Ответить", callback_data=f"chat:reply:{chat.id}"),
                    InlineKeyboardButton(text="📷 Фото", callback_data=f"chat:photo:{chat.id}"),
                ]
            ])
            await self._notify(
                account.tg_user_id,
                f"💬 <b>Новое сообщение{type_badge}</b>\n"
                f"От: <b>{html.escape(str(sender_name))}</b>\n"
                f"{html.escape(display_text)[:3000]}"
                f"{images_info}",
                markup,
            )

        if self.plugins:
            await self.plugins.dispatch_message(account, client, self.bot, chat, message)

        if text:
            async with self.db() as session:
                rules = list((await session.scalars(
                    select(AutoReplyRule)
                    .where(AutoReplyRule.account_id == account.id, AutoReplyRule.enabled.is_(True))
                    .order_by(AutoReplyRule.id)
                )).all())
            lower = text.casefold()
            for rule in rules:
                if rule.trigger == "*" or rule.trigger.casefold() in lower:
                    await client.send_message(str(chat.id), rule.response, mark_chat_as_read=True)
                    break

    async def _notify(self, chat_id: int, text: str, reply_markup: Any = None) -> None:
        try:
            await self.bot.send_message(
                chat_id, text, parse_mode="HTML", reply_markup=reply_markup
            )
        except Exception:
            logger.exception("Telegram notification failed for %s", chat_id)
