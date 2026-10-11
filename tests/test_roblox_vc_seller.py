import asyncio
import uuid
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, PlayerokAccount
from app.roblox_vc_manager import (
    DEFAULT_ROBLOX_DELIVERY_TEMPLATE,
    RobloxDeal,
    RobloxLotRule,
    RobloxStock,
    clean_roblox_totp_secret,
    format_roblox_delivery_message,
    generate_roblox_totp,
    parse_roblox_bulk_lines,
)
from plugins.roblox_vc_seller import on_deal, on_message
from playerokapi.enums import ItemDealStatuses


def test_roblox_totp_and_bulk_parse():
    # 1. Проверка парсинга с 2FA и без 2FA
    raw_lines = """
    # Комментарий
    roblox_user1:pass123
    roblox_user2:pass456:JBSWY3DPEHPK3PXP
    roblox_user3:pass789:JBSWY3DPEHPK3PXP:500
    roblox_user4:pass321::аккаунт без 2FA
    """
    parsed = parse_roblox_bulk_lines(raw_lines)
    assert len(parsed) == 4

    assert parsed[0]["username"] == "roblox_user1"
    assert parsed[0]["password"] == "pass123"
    assert parsed[0]["totp_secret"] is None

    assert parsed[1]["username"] == "roblox_user2"
    assert parsed[1]["password"] == "pass456"
    assert parsed[1]["totp_secret"] == "JBSWY3DPEHPK3PXP"

    assert parsed[2]["username"] == "roblox_user3"
    assert parsed[2]["robux_balance"] == 500

    assert parsed[3]["username"] == "roblox_user4"
    assert parsed[3]["totp_secret"] is None
    assert parsed[3]["comment"] == "аккаунт без 2FA"

    # 2. Проверка генерации TOTP
    clean = clean_roblox_totp_secret("jbsw y3dp-ehpk 3pxp")
    assert clean == "JBSWY3DPEHPK3PXP"

    code, rem = generate_roblox_totp(clean)
    assert code is not None
    assert len(code) == 6
    assert 0 <= rem <= 30

    # Без ключа
    c_none, r_none = generate_roblox_totp(None)
    assert c_none is None


def test_format_roblox_delivery_message():
    # С 2FA
    text_with_2fa = format_roblox_delivery_message(
        template=DEFAULT_ROBLOX_DELIVERY_TEMPLATE,
        lot_name="Roblox 13+ VC Account",
        username="cool_roblox_player",
        password="secure_password",
        totp_secret="JBSWY3DPEHPK3PXP",
        robux_balance=250,
        comment="Не меняйте почту сразу",
    )
    assert "cool_roblox_player" in text_with_2fa
    assert "secure_password" in text_with_2fa
    assert "JBSWY3DPEHPK3PXP" in text_with_2fa
    assert "250 R$" in text_with_2fa
    assert "#2fa" in text_with_2fa

    # Без 2FA
    text_no_2fa = format_roblox_delivery_message(
        template=DEFAULT_ROBLOX_DELIVERY_TEMPLATE,
        lot_name="Roblox No 2FA VC",
        username="simple_roblox_user",
        password="simple_pass",
        totp_secret=None,
    )
    assert "simple_roblox_user" in text_no_2fa
    assert "Отключен (вход без доп. кодов)" in text_no_2fa


@pytest.mark.asyncio
async def test_roblox_vc_on_deal_workflow():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    account_id = uuid.uuid4()
    async with session_factory() as session:
        account = PlayerokAccount(
            id=account_id,
            tg_user_id=12345678,
            playerok_user_id="pok_user_roblox",
            username="roblox_seller",
            cookies_encrypted="cookie",
            user_agent="Mozilla/5.0",
        )
        session.add(account)

        # Аккаунт с 2FA
        stock_with_2fa = RobloxStock(
            account_id=account_id,
            username="rbx_vip_vc",
            password_encrypted="rbx_pass_1",
            totp_secret_encrypted="JBSWY3DPEHPK3PXP",
            robux_balance=100,
            status="available",
        )
        session.add(stock_with_2fa)

        rule = RobloxLotRule(
            account_id=account_id,
            lot_id="roblox_lot_1",
            lot_title="Roblox Voice Chat 13+",
            delivery_template=DEFAULT_ROBLOX_DELIVERY_TEMPLATE,
            auto_sent=True,
            enabled=True,
        )
        session.add(rule)
        await session.commit()

    sent_messages = []
    deal_statuses = []
    notified_messages = []

    class MockClient:
        async def send_message(self, chat_id: str, text: str, mark_chat_as_read: bool = True):
            sent_messages.append((chat_id, text))

        async def set_deal_status(self, deal_id: str, status: ItemDealStatuses):
            deal_statuses.append((deal_id, status))

    class MockContext:
        def __init__(self, acc, sf):
            self.account = acc
            self.db = sf
            self.client = MockClient()
            self.config = {"auto_complete": True, "notify_seller": True}
            self.cipher = None

        async def send_chat(self, chat_id: str, text: str):
            await self.client.send_message(chat_id, text)

        async def notify(self, text: str):
            notified_messages.append(text)

    class DummyObj:
        pass

    deal = DummyObj()
    deal.id = "deal_rbx_123"
    deal.chat = DummyObj()
    deal.chat.id = "chat_rbx_456"
    deal.item = DummyObj()
    deal.item.id = "roblox_lot_1"
    deal.item.name = "Roblox Voice Chat 13+"

    ctx = MockContext(account, session_factory)

    # 1. Проверяем выдачу заказа
    await on_deal(ctx, deal)
    assert len(sent_messages) == 1
    assert "rbx_vip_vc" in sent_messages[0][1]
    assert "JBSWY3DPEHPK3PXP" in sent_messages[0][1]
    assert len(deal_statuses) == 1
    assert deal_statuses[0] == ("deal_rbx_123", ItemDealStatuses.SENT)

    # 2. Проверяем запрос кода #2fa от покупателя в чате
    help_msg = DummyObj()
    help_msg.text = "#2fa"
    chat_obj = DummyObj()
    chat_obj.id = "chat_rbx_456"

    sent_messages.clear()
    await on_message(ctx, chat_obj, help_msg)
    assert len(sent_messages) == 1
    assert "Ваш код двухфакторной аутентификации Roblox" in sent_messages[0][1]

    await engine.dispose()
