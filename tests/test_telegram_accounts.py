from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.db import init_db, make_engine, make_session_factory
from app.models import PlayerokAccount, TelegramUser
from app.plugin_system import PluginManager, PluginSpec
from app.telegram_accounts_manager import (
    extract_5digit_code,
    format_delivery_message,
    format_code_message,
    format_code_waiting_message,
    format_code_repeat_message,
    TelegramAccountLotRule,
    TelegramAccountStock,
    TelegramAccountDeal,
)
from app.worker import WorkerManager
from playerokapi.enums import ItemDealDirections, ItemDealStatuses
from plugins import telegram_accounts


def test_no_forbidden_word_account_in_messages():
    """Проверяем, что в сообщениях покупателю категорически отсутствует слово 'аккаунт'/'account'."""
    delivery = format_delivery_message("Россия", "+79991234567")
    code_msg = format_code_message("12345")
    waiting_msg = format_code_waiting_message("+79991234567")
    repeat_msg = format_code_repeat_message("12345")

    for msg in (delivery, code_msg, waiting_msg, repeat_msg):
        lower = msg.lower()
        assert "аккаунт" not in lower, f"Слово 'аккаунт' обнаружено в сообщении: {msg}"
        assert "account" not in lower, f"Слово 'account' обнаружено в сообщении: {msg}"
        assert "акк" not in lower.split(), f"Слово 'акк' обнаружено в сообщении: {msg}"


def test_extract_5digit_code():
    """Проверяем извлечение 5-значного кода из различных форматов служебных сообщений Telegram."""
    assert extract_5digit_code("Telegram: Login code: 12345. Do not give this code to anyone.") == "12345"
    assert extract_5digit_code("Ваш проверочный код для входа: 98765.") == "98765"
    assert extract_5digit_code("Код подтверждения: 54321") == "54321"
    assert extract_5digit_code("Login code: 789 01") == "78901"
    assert extract_5digit_code("Login code: 234-56") == "23456"
    assert extract_5digit_code("Просто текст без цифр") is None
    assert extract_5digit_code("Номер телефона +79991234567 не код") is None
    assert extract_5digit_code("1234 — не 5 знаков") is None
    assert extract_5digit_code("123456 — 6 знаков") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("found_in_chat, existing_database", [(False, False), (True, False), (False, True), (True, True)])
async def test_paid_sale_recovers_missing_initial_message(
    tmp_path, monkeypatch, found_in_chat, existing_database,
):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/stock.db")
    await init_db(engine)
    db = make_session_factory(engine)
    try:
        async with db() as session:
            session.add(TelegramUser(id=10))
            await session.flush()
            account = PlayerokAccount(
                tg_user_id=10, playerok_user_id="seller", username="seller",
                cookies_encrypted="fixture", user_agent="test",
                worker_initialized=True, settings={"auto_confirm": True},
            )
            session.add(account)
            await session.flush()
            session.add(TelegramAccountLotRule(
                account_id=account.id, lot_id="lot", lot_title="Лот", country="Страна",
            ))
            session.add_all([
                TelegramAccountStock(account_id=account.id, country="Страна", phone=phone, session_encrypted="fixture")
                for phone in ("test-number-1", "test-number-2")
            ])
            await session.commit()

        item = NS(id="lot", name="Лот", price=100)
        sale = NS(id="sale", status=ItemDealStatuses.PAID, direction=None, item=item, chat=None)
        purchase = NS(id="purchase", status=ItemDealStatuses.PAID, direction=None, item=item, chat=NS(id="other-chat"))
        client = NS(
            get_deals=AsyncMock(side_effect=lambda direction, **_: NS(
                deals=[sale if direction is ItemDealDirections.OUT else purchase],
            )),
            get_deal=AsyncMock(return_value=NS(chat=NS(id="buyer-chat"), user=None)),
            get_chats=AsyncMock(return_value=NS(chats=[])),
            get_my_reviews=AsyncMock(return_value=NS(reviews=[])),
            send_message=AsyncMock(side_effect=[TimeoutError(), NS(id="sent")]),
            update_deal=AsyncMock(),
            call=AsyncMock(),
        )
        seller = NS(id=account.playerok_user_id)
        text = format_delivery_message("Страна", "test-number-1")
        incoming_copy = NS(text=text, user=NS(id="buyer"))
        seller_copy = NS(text=text, user=seller)
        client.call.return_value = NS(
            messages=[seller_copy] if found_in_chat else [incoming_copy], page_info=None,
        )
        manager = PluginManager(db)
        spec = PluginSpec(
            "telegram_accounts", "Telegram Accounts", "1", "", "",
            telegram_accounts.PLUGIN_META["settings"], telegram_accounts,
        )
        manager.plugins[spec.id] = spec
        worker = WorkerManager(
            NS(send_message=AsyncMock()), db, NS(get_client=AsyncMock(return_value=client)),
            None, 15, manager,
        )
        await worker._poll(account)
        client.send_message.assert_awaited_once_with("buyer-chat", text, mark_chat_as_read=False)
        async with db() as session:
            reservation = await session.scalar(select(TelegramAccountDeal))
            assert reservation.deal_id == sale.id and reservation.intro_sent is False
            if existing_database:
                await session.execute(update(TelegramAccountDeal).values(intro_sent=None, intro_attempted_at=None))
                await session.commit()

        monkeypatch.setattr(telegram_accounts, "INTRO_RETRY_DELAY", timedelta(0))
        await worker._poll(account)
        await worker._poll(account)
        assert client.send_message.await_count == (1 if found_in_chat else 2)
        client.call.assert_awaited_once_with("get_chat_messages", "buyer-chat", count=24, after_cursor=None)
        client.update_deal.assert_not_awaited()
        async with db() as session:
            records = list(await session.scalars(select(TelegramAccountDeal)))
            assert len(records) == 1 and records[0].intro_sent is True
            stock = list(await session.scalars(select(TelegramAccountStock).order_by(TelegramAccountStock.id)))
            assert [item.status for item in stock] == ["issued", "available"]
    finally:
        await engine.dispose()
