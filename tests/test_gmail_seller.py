from __future__ import annotations

import uuid
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.crypto import SecretCipher
from app.gmail_accounts_manager import (
    GmailDeal,
    GmailLotRule,
    GmailStock,
    clean_totp_secret,
    format_delivery_message,
    format_totp_reply,
    generate_totp_code,
)
from app.models import Base, PlayerokAccount
from playerokapi.enums import ItemDealStatuses
from plugins.gmail_seller import on_deal, on_message


def test_clean_totp_secret_and_generator():
    secret = "jb sw-y3dp ehpk 3pxp"
    cleaned = clean_totp_secret(secret)
    assert cleaned == "JBSWY3DPEHPK3PXP"

    code, rem = generate_totp_code(cleaned)
    assert code is not None
    assert len(code) == 6
    assert 0 <= rem <= 30

    empty_code, _ = generate_totp_code("")
    assert empty_code is None


def test_format_delivery_message_with_and_without_totp():
    msg_with_totp = format_delivery_message("test@gmail.com", "pass123", "JBSWY3DPEHPK3PXP")
    assert "test@gmail.com" in msg_with_totp
    assert "pass123" in msg_with_totp
    assert "JBSWY3DPEHPK3PXP" in msg_with_totp
    assert "#2fa" in msg_with_totp

    msg_no_totp = format_delivery_message("test@gmail.com", "pass123", None)
    assert "test@gmail.com" in msg_no_totp
    assert "pass123" in msg_no_totp
    assert "Отключен" in msg_no_totp
    assert "#2fa" not in msg_no_totp


@pytest.mark.asyncio
async def test_gmail_seller_flow_with_and_without_totp():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    account_id = uuid.uuid4()
    account = PlayerokAccount(
        id=account_id,
        tg_user_id=12345678,
        playerok_user_id="pok_user_gmail",
        username="gmail_seller",
        cookies_encrypted="cookie",
        user_agent="Mozilla/5.0",
    )

    cipher = SecretCipher("dummy_token_123456789")

    async with session_factory() as session:
        session.add(account)

        # 1. Аккаунт без TOTP
        session.add(
            GmailStock(
                account_id=account_id,
                email="no_totp@gmail.com",
                password_encrypted=cipher.encrypt("pass_simple"),
                totp_secret_encrypted=None,
                status="available",
            )
        )
        # 2. Аккаунт с TOTP
        session.add(
            GmailStock(
                account_id=account_id,
                email="with_totp@gmail.com",
                password_encrypted=cipher.encrypt("pass_secure"),
                totp_secret_encrypted=cipher.encrypt("JBSWY3DPEHPK3PXP"),
                status="available",
            )
        )

        rule = GmailLotRule(
            account_id=account_id,
            lot_id="gmail_lot_1",
            lot_title="Почта Gmail Аккаунт",
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
            self.config = {"auto_complete": True}
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

    # Тест 1: Покупка аккаунта без TOTP
    deal1 = DummyObj()
    deal1.id = "deal_gmail_1"
    deal1.chat = DummyObj()
    deal1.chat.id = "chat_g_1"
    deal1.item = DummyObj()
    deal1.item.id = "gmail_lot_1"
    deal1.item.name = "Почта Gmail Аккаунт"

    await on_deal(ctx, deal1)
    assert len(sent_messages) == 1
    assert "no_totp@gmail.com" in sent_messages[0][1]
    assert "pass_simple" in sent_messages[0][1]
    assert "Отключен" in sent_messages[0][1]
    assert len(deal_updates) == 1
    assert deal_updates[0] == ("deal_gmail_1", ItemDealStatuses.SENT)

    # Проверяем запрос #2fa в чате сделки без TOTP
    req_msg1 = DummyObj()
    req_msg1.text = "#2fa"
    await on_message(ctx, deal1.chat, req_msg1)
    assert len(sent_messages) == 2
    assert "2FA не включен" in sent_messages[1][1]

    # Тест 2: Покупка аккаунта с TOTP
    deal2 = DummyObj()
    deal2.id = "deal_gmail_2"
    deal2.chat = DummyObj()
    deal2.chat.id = "chat_g_2"
    deal2.item = DummyObj()
    deal2.item.id = "gmail_lot_1"
    deal2.item.name = "Почта Gmail Аккаунт"

    await on_deal(ctx, deal2)
    assert len(sent_messages) == 3
    assert "with_totp@gmail.com" in sent_messages[2][1]
    assert "JBSWY3DPEHPK3PXP" in sent_messages[2][1]
    assert len(deal_updates) == 2
    assert deal_updates[1] == ("deal_gmail_2", ItemDealStatuses.SENT)

    # Проверяем запрос #2fa в чате сделки с TOTP
    req_msg2 = DummyObj()
    req_msg2.text = "#2fa"
    await on_message(ctx, deal2.chat, req_msg2)
    assert len(sent_messages) == 4
    assert "Ваш код подтверждения:" in sent_messages[3][1]
