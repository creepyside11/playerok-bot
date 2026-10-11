from __future__ import annotations

import json
import uuid
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.crypto import SecretCipher
from app.kosell_manager import (
    DEFAULT_KOSELL_DELIVERY_TEMPLATE,
    KosellAccountConfig,
    KosellApiError,
    KosellClient,
    KosellDeal,
    KosellLotRule,
    format_kosell_code_reply,
    format_kosell_delivery_message,
)
from app.models import Base, PlayerokAccount
from playerokapi.enums import ItemDealStatuses
from plugins.kosell_seller import on_deal, on_message


def test_format_kosell_delivery_message():
    tmpl = DEFAULT_KOSELL_DELIVERY_TEMPLATE
    msg = format_kosell_delivery_message(
        template=tmpl,
        lot_name="Аренда CS2 Prime 3 часа",
        product_name="Counter-Strike 2",
        duration_hours=3,
        steam_login="steam_gamer_99",
        steam_password="SteamPassword123!",
        expires_at="11.10.2026 18:00 UTC",
    )
    assert "Counter-Strike 2" in msg
    assert "steam_gamer_99" in msg
    assert "SteamPassword123!" in msg
    assert "3 ч." in msg
    assert "#код" in msg
    assert "Steam Guard" in msg


def test_format_kosell_code_reply():
    reply = format_kosell_code_reply("G7X89", 25)
    assert "G7X89" in reply
    assert "25 сек" in reply
    assert "Steam Guard" in reply


@pytest.mark.asyncio
async def test_kosell_seller_on_deal_and_steam_guard():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    account_id = uuid.uuid4()
    account = PlayerokAccount(
        id=account_id,
        tg_user_id=12345678,
        playerok_user_id="pok_user_kosell",
        username="kosell_seller",
        cookies_encrypted="cookie",
        user_agent="Mozilla/5.0",
    )

    cipher = SecretCipher("dummy_token_123456789")

    async with session_factory() as session:
        session.add(account)

        # Конфиг KOSell
        session.add(
            KosellAccountConfig(
                account_id=account_id,
                api_key_encrypted=cipher.encrypt("test_kosell_api_key"),
                currency="RUB",
                auto_sent=True,
                notify_seller=True,
            )
        )

        # Привязка лота к игре (CS2, product_id=730)
        rule = KosellLotRule(
            account_id=account_id,
            lot_id="pok_lot_cs2",
            lot_title="Аренда CS2 с праймом 1 час",
            product_id=730,
            product_name="Counter-Strike 2",
            duration_hours=1,
            auto_sent=True,
            enabled=True,
        )
        session.add(rule)
        await session.commit()

    sent_messages = []
    deal_updates = []
    notified = []

    class MockContext:
        def __init__(self, acc, sf):
            self.account = acc
            self.db = sf
            self.client = None
            self.config = {"auto_complete": True, "notify_seller": True}
            self.cipher = cipher

        async def send_chat(self, chat_id: str, text: str):
            sent_messages.append((chat_id, text))

        async def update_deal(self, deal_id: str, status: ItemDealStatuses):
            deal_updates.append((deal_id, status))

        async def notify(self, text: str):
            notified.append(text)

    class DummyObj:
        pass

    ctx = MockContext(account, session_factory)

    deal = DummyObj()
    deal.id = "deal_ks_123"
    deal.chat = DummyObj()
    deal.chat.id = "chat_ks_456"
    deal.item = DummyObj()
    deal.item.id = "pok_lot_cs2"
    deal.item.name = "Аренда CS2 с праймом 1 час"

    # Мокаем вызовы KosellClient
    import plugins.kosell_seller as plugin_mod

    class FakeKosellClient:
        def __init__(self, api_key):
            self.api_key = api_key

        async def create_rental(self, product_id, duration_hours, currency, idempotency_key):
            return {
                "rental_uid": "rent_uid_abc123",
                "product_id": product_id,
                "product_name": "Counter-Strike 2",
                "steam_login": "rented_steam_login",
                "price_paid": 45.0,
                "price_rub": 45.0,
                "duration_hours": duration_hours,
                "expires_at": "2026-10-11T15:30:00Z",
            }

        async def get_credentials(self, rental_uid):
            return {
                "steam_login": "rented_steam_login",
                "steam_password": "super_steam_pass!",
            }

        async def get_balance(self):
            return {
                "balance_rub": 550.0,
                "balance_usd": 6.0,
                "username": "tester",
            }

        async def get_steam_code(self, rental_uid):
            return {
                "success": True,
                "code": "C77XX",
                "expires_in": 28,
            }

    plugin_mod.KosellClient = FakeKosellClient

    # 1. Проверяем выдачу аренды при оплате заказа
    await on_deal(ctx, deal)

    assert len(sent_messages) == 1
    assert "rented_steam_login" in sent_messages[0][1]
    assert "super_steam_pass!" in sent_messages[0][1]
    assert "Counter-Strike 2" in sent_messages[0][1]
    assert len(deal_updates) == 1
    assert deal_updates[0] == ("deal_ks_123", ItemDealStatuses.SENT)
    assert len(notified) == 1
    assert "Успешная аренда Steam через KOSell!" in notified[0]

    # 2. Проверяем запрос Steam Guard кода покупателем по команде #код
    msg = DummyObj()
    msg.text = "#код"
    await on_message(ctx, deal.chat, msg)

    assert len(sent_messages) == 2
    assert "C77XX" in sent_messages[1][1]
    assert "Steam Guard" in sent_messages[1][1]
    assert len(notified) == 2
    assert "Покупатель запросил Steam Guard код!" in notified[1]
