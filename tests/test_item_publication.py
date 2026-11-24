from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app import handlers, items_manager_ui
from app.crypto import SecretCipher
from app.db import init_db, make_engine, make_session_factory
from app.item_publication import (
    PublicationChanged,
    confirm_publication,
    parse_discount_price,
    publication_tariffs,
)
from app.models import DeliveryRule, DeliveryStock, PlayerokAccount, TelegramUser
from app.states import ItemCreate
from app.worker import WorkerManager
from playerokapi.enums import ItemStatuses


def item(price=900, status=ItemStatuses.DRAFT):
    return SimpleNamespace(id="lot", name="Товар", price=price, raw_price=1000, status=status)


def client_with_tariffs(balance="100.50"):
    client = AsyncMock()
    client.get_item.return_value = item()
    client.get_item_priority_statuses.return_value = [
        SimpleNamespace(id="paid", name="Премиум", price="49.75"),
        SimpleNamespace(id="unknown", name="Неизвестный", price=None),
        SimpleNamespace(id="free", name="Стандарт", price=0),
    ]
    client.viewer.return_value = SimpleNamespace(available_balance=balance)
    client.publish_item.return_value = item(status=ItemStatuses.PENDING_APPROVAL)
    return client


def fsm():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=21, user_id=21))


def test_discount_accepts_a_reduction_or_reset():
    assert parse_discount_price(" 950 ₽", 1000) == 950
    assert parse_discount_price("-", 1000) == 1000
    with pytest.raises(ValueError):
        parse_discount_price("980", 1000)


@pytest.mark.asyncio
async def test_free_publication_uses_zero_fee_even_if_premium_is_listed_first():
    client = client_with_tariffs(balance=None)
    options = await publication_tariffs(client, "lot", 1000)
    assert [option.id for option in options] == ["free", "paid"]

    result = await confirm_publication(client, "lot", (900, 1000), options[0], None)

    assert result.status == ItemStatuses.PENDING_APPROVAL
    client.get_item_priority_statuses.assert_awaited_with("lot", 1000)
    client.viewer.assert_not_awaited()
    client.publish_item.assert_awaited_once_with("lot", "free")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["price", "balance", "fee", "status"])
async def test_paid_confirmation_requires_a_current_quote(change):
    client = client_with_tariffs()
    paid = (await publication_tariffs(client, "lot", 1000))[1]
    if change == "price":
        client.get_item.return_value = item(price=850)
    elif change == "balance":
        client.viewer.return_value.available_balance = "90.50"
    elif change == "fee":
        client.get_item_priority_statuses.return_value[0].price = "50"
    else:
        client.get_item.return_value = item(status=ItemStatuses.APPROVED)

    with pytest.raises(PublicationChanged):
        await confirm_publication(client, "lot", (900, 1000), paid, Decimal("100.50"))
    client.publish_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_shows_amount_and_balance_and_consumes_confirmation(monkeypatch):
    client = client_with_tariffs()
    account = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(items_manager_ui, "require_account", AsyncMock(return_value=account))
    monkeypatch.setattr(handlers, "services", SimpleNamespace(gateway=SimpleNamespace(
        get_client=AsyncMock(return_value=client),
    )))
    state = fsm()
    call = MagicMock(spec=CallbackQuery)
    call.answer = AsyncMock()
    call.message = MagicMock(spec=Message)
    call.message.edit_text = AsyncMock()
    call.data = "item:publish:lot"
    await items_manager_ui.item_publish_action(call, state)

    quote = (await state.get_data())["publication"]
    rendered = call.message.edit_text.await_args.args[0]
    assert "100,5 ₽" in rendered and "49,75 ₽" in rendered and "50,75 ₽" in rendered
    client.publish_item.assert_not_awaited()

    call.data = f"itempub:pick:{quote['token']}:1"
    await items_manager_ui.item_publish_pick(call, state)
    first_confirmation = call.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    await items_manager_ui.item_publish_pick(call, state)
    second_confirmation = call.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    call.data = first_confirmation
    await items_manager_ui.item_publish_confirm(call, state)
    client.publish_item.assert_not_awaited()

    call.data = second_confirmation
    await items_manager_ui.item_publish_confirm(call, state)
    await items_manager_ui.item_publish_confirm(call, state)
    client.publish_item.assert_awaited_once_with("lot", "paid")


@pytest.mark.asyncio
async def test_draft_keeps_native_data_and_buyer_message_before_publication(monkeypatch):
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    try:
        await init_db(engine)
        db = make_session_factory(engine)
        account = PlayerokAccount(
            id=uuid4(), tg_user_id=21, playerok_user_id="seller",
            username="seller", cookies_encrypted="test", user_agent="test",
        )
        async with db() as session:
            session.add(TelegramUser(id=21))
            await session.flush()
            session.add(account)
            await session.commit()

        client = client_with_tariffs()
        client.create_item.return_value = item(price=1000)
        client.get_item.return_value = item(price=1000)
        client.update_item.return_value = item()
        cipher = SecretCipher("test-token")
        monkeypatch.setattr(handlers, "services", SimpleNamespace(
            db=db, cipher=cipher, gateway=SimpleNamespace(get_client=AsyncMock(return_value=client))
        ))
        state = fsm()
        await state.set_state(ItemCreate.image)
        await state.set_data({
            "account_id": str(account.id), "category_id": "category",
            "item_name": "Товар", "item_price": 1000, "item_description": "Описание",
            "item_data": "Цифровой товар", "buyer_message": "Инструкция с {фигурными скобками}",
        })
        message = MagicMock(spec=Message)
        message.from_user = SimpleNamespace(id=21)
        message.answer = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
        await handlers.create_draft(message, state, 21)

        assert client.create_item.await_args.kwargs["comment"] == "Цифровой товар"
        assert client.create_item.await_args.kwargs["obtaining_type_id"] is None
        assert await state.get_state() == ItemCreate.priority.state
        client.publish_item.assert_not_awaited()
        async with db() as session:
            rule = await session.scalar(select(DeliveryRule))
            stock = await session.scalar(select(DeliveryStock))
            assert rule.item_id == "lot" and rule.message_template == "{stock}"
            assert cipher.decrypt(stock.payload_encrypted) == "Инструкция с {фигурными скобками}"

        await state.set_state(ItemCreate.discount_price)
        message.text = "900"
        await handlers.item_create_discount_value(message, state)
        client.update_item.assert_awaited_once_with("lot", price=900)
        assert (await state.get_data())["item_sale_price"] == 900

        call = MagicMock(spec=CallbackQuery)
        call.answer = AsyncMock()
        call.message = MagicMock(spec=Message)
        call.message.edit_text = AsyncMock()
        await handlers.leave_draft(call, state)
        assert await state.get_state() is None
        client.publish_item.assert_not_awaited()

        bot = AsyncMock()
        worker = WorkerManager(bot, db, None, cipher, 10)
        for deal_id in ("deal-1", "deal-1", "deal-2"):
            deal = SimpleNamespace(id=deal_id, chat=SimpleNamespace(id=deal_id))
            assert await worker._delivery(account, client, deal, "lot") is True
        assert [args.args[1] for args in client.send_message.await_args_list] == [
            "Инструкция с {фигурными скобками}", "Инструкция с {фигурными скобками}",
        ]
    finally:
        await engine.dispose()
