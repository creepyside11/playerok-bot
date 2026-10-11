import asyncio
import uuid
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, PlayerokAccount
from app.supercell_manager import (
    DEFAULT_SUPERCELL_DELIVERY_TEMPLATE,
    SUPERCELL_GAMES,
    SupercellDeal,
    SupercellLotRule,
    SupercellStock,
    format_supercell_delivery_message,
    parse_supercell_bulk_lines,
)
from plugins.supercell_seller import on_deal, on_message
from playerokapi.enums import ItemDealStatuses


def test_extract_supercell_code_and_imap_candidates():
    from app.supercell_manager import extract_supercell_code_from_text, get_imap_host_candidates

    text1 = "Your Supercell ID verification code is 491029. Enter it in Brawl Stars."
    assert extract_supercell_code_from_text(text1) == "491029"

    text2 = "Код подтверждения: 123456"
    assert extract_supercell_code_from_text(text2) == "123456"

    # Проверка распознавания доменов Firstmail / eblanmail / notletters
    hosts_firstmail = get_imap_host_candidates("acc1@firstmail.ltd")
    assert "imap.firstmail.ltd" in hosts_firstmail

    hosts_eblan = get_imap_host_candidates("acc2@eblanmail.com")
    assert "imap.firstmail.ltd" in hosts_eblan

    hosts_custom_eblan = get_imap_host_candidates("acc3@super-eblanmail-domain.xyz")
    assert "imap.firstmail.ltd" in hosts_custom_eblan

    hosts_notletters = get_imap_host_candidates("acc4@notletters.com")
    assert "imap.notletters.com" in hosts_notletters


def test_format_supercell_delivery_message():
    text = format_supercell_delivery_message(
        template=DEFAULT_SUPERCELL_DELIVERY_TEMPLATE,
        game_key="brawl_stars",
        lot_name="Аккаунт 30000 кубков + Леон",
        email="supercell.test@gmail.com",
        password="secret_password123",
        trophies=31450,
        brawlers_count=65,
        recovery_codes="ABC1-DEF2-GHI3",
        has_mail_access=True,
        comment="Не меняйте почту первые 3 дня",
    )

    assert "Brawl Stars 🥊" in text
    assert "supercell.test@gmail.com" in text
    assert "secret_password123" in text
    assert "31,450" in text
    assert "65" in text
    assert "ABC1-DEF2-GHI3" in text
    assert "Не меняйте почту первые 3 дня" in text


def test_parse_supercell_bulk_lines():
    raw_lines = """
    # Комментарий
    brawl1@gmail.com:pass123:CODE1-CODE2:50000
    clash@mail.ru:cocPass:REC-CODE-99:15
    hayday@yahoo.com;secretPass
    royale@gmail.com\troyalePass\tREC1
    """

    parsed = parse_supercell_bulk_lines(raw_lines)
    assert len(parsed) == 4
    assert parsed[0]["email"] == "brawl1@gmail.com"
    assert parsed[0]["password"] == "pass123"
    assert parsed[0]["recovery_codes"] == "CODE1-CODE2"
    assert parsed[0]["comment"] == "50000"

    assert parsed[1]["email"] == "clash@mail.ru"
    assert parsed[1]["password"] == "cocPass"
    assert parsed[1]["recovery_codes"] == "REC-CODE-99"
    assert parsed[1]["comment"] == "15"

    assert parsed[2]["email"] == "hayday@yahoo.com"
    assert parsed[2]["password"] == "secretPass"

    assert parsed[3]["email"] == "royale@gmail.com"
    assert parsed[3]["password"] == "royalePass"
    assert parsed[3]["recovery_codes"] == "REC1"


@pytest.mark.asyncio
async def test_detect_and_verify_email_host_order():
    from app.supercell_manager import get_imap_host_candidates, detect_and_verify_email_host
    import unittest.mock as mock

    # Проверяем, что для неизвестного домена firstmail идет первым кандидатом, а notletters вторым
    candidates = get_imap_host_candidates("custom_user@random-unregistered-domain.biz")
    assert candidates[0] == "imap.firstmail.ltd"
    assert candidates[1] == "imap.notletters.com"

    # Мокаем проверку: на Firstmail неудача (False), на Notletters успех (True)
    def mock_test_login(email, pwd, host, timeout):
        if host == "imap.firstmail.ltd":
            return False
        if host == "imap.notletters.com":
            return True
        return False

    with mock.patch("app.supercell_manager._test_imap_login_sync", side_effect=mock_test_login):
        host = await detect_and_verify_email_host("test@eblanmail-custom.org", "pass123")
        assert host == "imap.notletters.com"


@pytest.mark.asyncio
async def test_supercell_on_deal_workflow():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    account_id = uuid.uuid4()
    async with session_factory() as session:
        account = PlayerokAccount(
            id=account_id,
            tg_user_id=12345678,
            playerok_user_id="pok_user_1",
            username="supercell_seller",
            cookies_encrypted="cookie",
            user_agent="Mozilla/5.0",
        )
        session.add(account)

        # Добавляем аккаунт Brawl Stars на склад
        stock = SupercellStock(
            account_id=account_id,
            game_key="brawl_stars",
            email="bs_pro@gmail.com",
            password_encrypted="bs_pass_secure",
            trophies=25000,
            brawlers_count=55,
            recovery_codes_encrypted="REC-BS-123",
            status="available",
        )
        session.add(stock)

        # Привязываем лот
        rule = SupercellLotRule(
            account_id=account_id,
            game_key="brawl_stars",
            lot_id="10101",
            lot_title="Brawl Stars 25k кубков",
            delivery_template=DEFAULT_SUPERCELL_DELIVERY_TEMPLATE,
            auto_sent=True,
            enabled=True,
        )
        session.add(rule)
        await session.commit()

    # Мокаем контекст выполнения плагина
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
    deal.id = "deal_777"
    deal.chat = DummyObj()
    deal.chat.id = "chat_999"
    deal.item = DummyObj()
    deal.item.id = "10101"
    deal.item.name = "Brawl Stars 25k кубков"

    ctx = MockContext(account, session_factory)

    # Запускаем обработку сделки
    await on_deal(ctx, deal)

    # Проверяем, что сообщение покупателю отправлено
    assert len(sent_messages) == 1
    chat_id, msg_text = sent_messages[0]
    assert chat_id == "chat_999"
    assert "bs_pro@gmail.com" in msg_text
    assert "bs_pass_secure" in msg_text
    assert "25,000" in msg_text
    assert "REC-BS-123" in msg_text

    # Проверяем автозавершение сделки в SENT
    assert len(deal_statuses) == 1
    assert deal_statuses[0] == ("deal_777", ItemDealStatuses.SENT)

    # Проверяем уведомление продавцу
    assert len(notified_messages) == 1
    assert "Успешная автовыдача Supercell ID!" in notified_messages[0]

    # Проверяем состояние БД: склад списан, сделка сохранена
    async with session_factory() as session:
        from sqlalchemy import select
        st = await session.get(SupercellStock, 1)
        assert st.status == "issued"
        assert st.issued_deal_id == "deal_777"
        assert st.issued_chat_id == "chat_999"

        d_record = await session.scalar(select(SupercellDeal).where(SupercellDeal.deal_id == "deal_777"))
        assert d_record is not None
        assert d_record.email == "bs_pro@gmail.com"
        assert d_record.deal_completed is True

    # Проверяем команду #code в чате (мокаем автополучение кода с почты)
    help_message = DummyObj()
    help_message.text = "#код"
    chat_obj = DummyObj()
    chat_obj.id = "chat_999"

    sent_messages.clear()
    import unittest.mock as mock
    with mock.patch("plugins.supercell_seller.fetch_supercell_auth_code", return_value="849201"):
        await on_message(ctx, chat_obj, help_message)
    assert len(sent_messages) == 1
    assert "849201" in sent_messages[0][1]
    assert "bs_pro@gmail.com" in sent_messages[0][1]

    # Проверяем команду #help
    help_message.text = "#help"
    sent_messages.clear()
    await on_message(ctx, chat_obj, help_message)
    assert len(sent_messages) == 1
    assert "Памятка по входу в Supercell ID" in sent_messages[0][1]
    assert "bs_pro@gmail.com" in sent_messages[0][1]
    assert "REC-BS-123" in sent_messages[0][1]

    await engine.dispose()
