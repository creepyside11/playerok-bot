import json
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.accounts_manager import (
    DEFAULT_DELIVERY_TEMPLATE,
    UniversalAccountDeal,
    UniversalAccountLotRule,
    UniversalAccountStock,
    UniversalGame,
    format_universal_delivery_message,
)
from app.crypto import SecretCipher
from plugins.universal_accounts import on_deal


def test_format_universal_delivery_message():
    tmpl = DEFAULT_DELIVERY_TEMPLATE
    msg = format_universal_delivery_message(
        template=tmpl,
        game_title="Black Russia",
        lot_name="Аккаунт с геликом и 15кк виртов",
        login="br_player777",
        password="SuperSecretPassword!",
        account_id="UID_998811",
        server="RED",
        extra="Пин-код от банка: 1234",
        comment="Вход через официальный лаунчер, привязка ВК свободна",
    )
    assert "Black Russia" in msg
    assert "br_player777" in msg
    assert "SuperSecretPassword!" in msg
    assert "UID_998811" in msg
    assert "RED" in msg
    assert "1234" in msg
    assert "официальный лаунчер" in msg


@pytest.mark.asyncio
async def test_universal_accounts_on_deal():
    cipher = SecretCipher("dummy_bot_token_123456789")
    pwd_enc = cipher.encrypt("MyPassword123")
    extra_enc = cipher.encrypt("Банк пин: 4321")

    # Mock Context & DB
    ctx = MagicMock()
    ctx.account = MagicMock(id=uuid.uuid4())
    ctx.cipher = cipher
    ctx.config = {"auto_complete": True, "notify_seller": True}

    sent_messages = []
    async def mock_send_chat(chat_id, text):
        sent_messages.append((chat_id, text))
    ctx.send_chat = mock_send_chat

    updated_deals = []
    async def mock_update_deal(deal_id, status):
        updated_deals.append((deal_id, status))
    ctx.update_deal = mock_update_deal

    notified = []
    async def mock_notify(text):
        notified.append(text)
    ctx.notify = mock_notify

    # Создаем mock deal
    mock_deal = MagicMock()
    mock_deal.id = "deal-uuid-111"
    mock_deal.chat = MagicMock(id="chat-uuid-222")
    mock_deal.item = MagicMock(id="lot-uuid-333", name="BR Аккаунт 15 lvl")

    # Настраиваем DB mock
    game = UniversalGame(id=1, account_id=ctx.account.id, title="Black Russia", slug="black_russia")
    rule = UniversalAccountLotRule(
        id=10,
        account_id=ctx.account.id,
        game_id=1,
        lot_id="lot-uuid-333",
        lot_title="BR Аккаунт 15 lvl",
        delivery_template=DEFAULT_DELIVERY_TEMPLATE,
        auto_sent=True,
        enabled=True,
    )
    stock = UniversalAccountStock(
        id=100,
        account_id=ctx.account.id,
        game_id=1,
        login="gamer_br",
        password_encrypted=pwd_enc,
        account_identifier="ID_12345",
        server="RED",
        additional_data_encrypted=extra_enc,
        comment="Без привязок",
        status="available",
    )

    class MockSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def scalars(self, stmt):
            m = MagicMock()
            m.all.return_value = [rule]
            return m
        async def scalar(self, stmt):
            str_stmt = str(stmt).lower()
            if "universal_account_deals" in str_stmt:
                return None  # не выдавалось ранее
            elif "count(" in str_stmt:
                return 0
            elif "universal_account_stock" in str_stmt:
                return stock
            return None
        async def get(self, model, ident):
            if model == UniversalGame:
                return game
            return None
        def add(self, obj):
            pass
        async def commit(self):
            pass

    ctx.db = lambda: MockSession()

    await on_deal(ctx, mock_deal)

    # Проверяем, что сообщение покупателю отправлено
    assert len(sent_messages) == 1
    chat_id, text = sent_messages[0]
    assert chat_id == "chat-uuid-222"
    assert "gamer_br" in text
    assert "MyPassword123" in text
    assert "ID_12345" in text
    assert "RED" in text

    # Проверяем, что заказ переведен в статус SENT
    assert len(updated_deals) == 1
    assert updated_deals[0][0] == "deal-uuid-111"

    # Проверяем уведомление продавцу
    assert len(notified) == 1
    assert "Выдан аккаунт Black Russia" in notified[0]
