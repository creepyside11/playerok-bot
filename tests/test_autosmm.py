from __future__ import annotations

import uuid
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.autosmm_manager import (
    AutoSmmConfig,
    AutoSmmLotRule,
    AutoSmmOrder,
    SmmApiClient,
)
from app.crypto import SecretCipher
from app.models import Base, PlayerokAccount
from playerokapi.enums import ItemDealStatuses
from plugins.autosmm import on_deal


@pytest.mark.asyncio
async def test_autosmm_on_deal_flow():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    account_id = uuid.uuid4()
    account = PlayerokAccount(
        id=account_id,
        tg_user_id=12345678,
        playerok_user_id="pok_user_smm",
        username="smm_seller",
        cookies_encrypted="cookie",
        user_agent="Mozilla/5.0",
    )

    cipher = SecretCipher("dummy_token_123456789")

    async with session_factory() as session:
        session.add(account)

        # Конфиг SMM
        session.add(
            AutoSmmConfig(
                account_id=account_id,
                api_url="https://smmway.com/api/v2",
                api_key_encrypted=cipher.encrypt("test_smm_api_key"),
                auto_sent=True,
                notify_seller=True,
            )
        )

        # Привязка лота
        rule = AutoSmmLotRule(
            account_id=account_id,
            lot_id="pok_lot_tg_subs",
            lot_title="1000 Подписчиков Telegram",
            service_id="150",
            service_name="Telegram Subscribers Fast",
            quantity=1000,
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
            self.config = {"auto_confirm_deal": True, "notify_tg": True}
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
    deal.id = "deal_smm_789"
    deal.chat = DummyObj()
    deal.chat.id = "chat_smm_123"
    deal.item = DummyObj()
    deal.item.id = "pok_lot_tg_subs"
    deal.item.name = "1000 Подписчиков Telegram"

    # Поле со ссылкой покупателя в сделке
    f1 = DummyObj()
    f1.value = "https://t.me/durov"
    deal.data_fields = [f1]

    # Мокаем SmmApiClient
    import plugins.autosmm as plugin_mod

    class FakeSmmClient:
        def __init__(self, api_url, api_key):
            self.api_url = api_url
            self.api_key = api_key

        async def create_order(self, service_id, link, quantity):
            assert service_id == "150"
            assert link == "https://t.me/durov"
            assert quantity == 1000
            return {"order": "998877"}

    plugin_mod.SmmApiClient = FakeSmmClient

    await on_deal(ctx, deal)

    # Проверяем отправку сообщения покупателю
    assert len(sent_messages) == 1
    assert "https://t.me/durov" in sent_messages[0][1]
    assert "1000 шт." in sent_messages[0][1]

    # Проверяем автозавершение сделки в SENT
    assert len(deal_updates) == 1
    assert deal_updates[0] == ("deal_smm_789", ItemDealStatuses.SENT)

    # Проверяем отчет продавцу в TG
    assert len(notified) == 1
    assert "#998877" in notified[0]

    # Проверяем запись в БД
    async with session_factory() as session:
        from sqlalchemy import select
        saved_order = await session.scalar(select(AutoSmmOrder).where(AutoSmmOrder.deal_id == "deal_smm_789"))
        assert saved_order is not None
        assert saved_order.smm_order_id == "998877"
        assert saved_order.quantity == 1000
