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
    PublicationUnconfirmed,
    confirm_publication,
    parse_discount_price,
    publication_tariffs,
)
from app.models import DeliveryRule, PlayerokAccount, TelegramUser
from app.states import ItemCreate, ItemEdit
from app.worker import WorkerManager
from playerokapi.enums import GameCategoryDataFieldTypes, ItemStatuses
from playerokapi.exceptions import RequestFailedError, RequestPlayerokError, RequestSendingError


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
@pytest.mark.parametrize("schema", ["text", "fields_and_text", "fields_with_message", "fields_with_comment"])
async def test_wizard_requires_native_buyer_message(monkeypatch, schema):
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

        fields = [SimpleNamespace(
            id="buyer_input", label="Поле покупателя", type=GameCategoryDataFieldTypes.OBTAINING_DATA,
            required=True,
        )]
        if schema != "text":
            fields.append(SimpleNamespace(
                id="code", label="Код товара", type=GameCategoryDataFieldTypes.ITEM_DATA, required=True,
            ))
        if schema in {"fields_with_message", "fields_with_comment"}:
            label = "Комментарий" if schema == "fields_with_comment" else "Сообщение покупателю после покупки"
            fields.append(SimpleNamespace(
                id="message", label=label,
                type=GameCategoryDataFieldTypes.ITEM_DATA, required=False,
            ))

        async def api(method, *args, **kwargs):
            if method == "get_game_category":
                return SimpleNamespace(options=[])
            assert method == "get_game_category_data_fields"
            assert args == ("category", None)
            assert kwargs["type"] == GameCategoryDataFieldTypes.ITEM_DATA
            return SimpleNamespace(data_fields=fields, page_info=None)

        client.call.side_effect = api
        monkeypatch.setattr(handlers, "services", SimpleNamespace(
            db=db, cipher=cipher, gateway=SimpleNamespace(get_client=AsyncMock(return_value=client)),
        ))
        state = fsm()
        await state.set_data({
            "account_id": str(account.id), "category_id": "category",
            "item_name": "Товар", "item_price": 1000, "item_description": "Описание",
        })
        message = MagicMock(spec=Message)
        message.from_user = SimpleNamespace(id=21)
        message.answer = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
        call = MagicMock(spec=CallbackQuery)
        call.answer = AsyncMock()
        call.message = message
        call.from_user = message.from_user
        await handlers._load_category_and_start_options(call, state, None, "Стандартный")

        if schema != "text":
            assert await state.get_state() == ItemCreate.data_fields.state
            message.text = "ABC-123"
            await handlers.item_fields(message, state)
        assert await state.get_state() == ItemCreate.buyer_message.state
        assert "Комментарий" in message.answer.await_args.args[0]
        message.text = "-"
        await handlers.item_create_buyer_message(message, state)
        assert await state.get_state() == ItemCreate.buyer_message.state
        await handlers.create_draft(message, state, 21)
        client.create_item.assert_not_awaited()

        message.text = "Инструкция после покупки"
        await handlers.item_create_buyer_message(message, state)
        assert await state.get_state() == ItemCreate.image.state
        await handlers.create_draft(message, state, 21)

        params = client.create_item.await_args.kwargs
        if schema in {"fields_with_message", "fields_with_comment"}:
            assert params["data_fields"] == {"code": "ABC-123", "message": "Инструкция после покупки"}
            assert params["comment"] is None
        else:
            assert params["comment"] == "Инструкция после покупки"
            assert params["data_fields"] == ({"code": "ABC-123"} if schema == "fields_and_text" else {})
        assert await state.get_state() == ItemCreate.priority.state
        assert (await state.get_data())["buyer_message"] is None
        client.publish_item.assert_not_awaited()

        await state.set_state(ItemCreate.discount_price)
        message.text = "900"
        await handlers.item_create_discount_value(message, state)
        client.update_item.assert_awaited_once_with("lot", price=900)
        assert (await state.get_data())["item_sale_price"] == 900

        call.message = MagicMock(spec=Message)
        call.message.edit_text = AsyncMock()
        await handlers.leave_draft(call, state)
        assert await state.get_state() is None
        client.publish_item.assert_not_awaited()

        async with db() as session:
            assert await session.scalar(select(DeliveryRule)) is None
            session.add(DeliveryRule(
                account_id=account.id, item_id="lot", mode="message",
                message_template="{stock}", enabled=True,
            ))
            await session.commit()

        worker = WorkerManager(AsyncMock(), db, None, cipher, 10)
        deal = SimpleNamespace(id="deal", chat=SimpleNamespace(id="chat"))
        assert await worker._delivery(account, client, deal, "lot") is None
        client.send_message.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_edit_buyer_message_preserves_native_category_fields(monkeypatch):
    account = SimpleNamespace(id=uuid4())
    client = AsyncMock()
    fields = [
        SimpleNamespace(
            id="code", label="Код товара", type=GameCategoryDataFieldTypes.ITEM_DATA, required=True, value="ABC",
        ),
        SimpleNamespace(
            id="message", label="Сообщение покупателю после оплаты",
            type=GameCategoryDataFieldTypes.ITEM_DATA, required=True, value="Прежнее сообщение",
        ),
    ]
    client.get_item.return_value = SimpleNamespace(
        id="lot", category=SimpleNamespace(id="category"),
        obtaining_type=SimpleNamespace(id="method"), data_fields=fields,
    )
    client.call.return_value = SimpleNamespace(data_fields=fields, page_info=None)
    monkeypatch.setattr(items_manager_ui, "active_account", AsyncMock(return_value=account))
    monkeypatch.setattr(items_manager_ui, "render_item_card", AsyncMock())
    monkeypatch.setattr(handlers, "services", SimpleNamespace(
        gateway=SimpleNamespace(get_client=AsyncMock(return_value=client))
    ))
    state = fsm()
    await state.set_state(ItemEdit.buyer_message)
    await state.set_data({"item_id": "lot", "account_id": str(account.id)})
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=21)
    message.answer = AsyncMock()
    message.text = "Новое сообщение"

    await items_manager_ui.item_save_buyer_message(message, state)

    client.update_item.assert_awaited_once_with(
        "lot", data_fields={"code": "ABC", "message": "Новое сообщение"}
    )
    assert await state.get_state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state_after_publish", [ItemStatuses.PENDING_APPROVAL, ItemStatuses.DRAFT])
async def test_ambiguous_publication_checks_actual_status_without_resending(state_after_publish):
    client = client_with_tariffs()
    client.get_item.side_effect = [item(), item(status=state_after_publish)]
    client.publish_item.side_effect = RequestSendingError("https://playerok.com/graphql", "timeout")
    free = (await publication_tariffs(client, "lot", 1000))[0]

    if state_after_publish == ItemStatuses.DRAFT:
        with pytest.raises(PublicationUnconfirmed) as result:
            await confirm_publication(client, "lot", (900, 1000), free, None)
        assert result.value.item.status == ItemStatuses.DRAFT
    else:
        result = await confirm_publication(client, "lot", (900, 1000), free, None)
        assert result.status == state_after_publish
    client.publish_item.assert_awaited_once_with("lot", "free")
    assert client.get_item.await_count == 2

def test_publication_details_include_nested_rejection_and_wire_reason():
    response = MagicMock(status_code=400, text="")
    response.json.return_value = {"errors": [
        {"message": "Bad Request", "extensions": {
            "code": "BAD_USER_INPUT", "originalError": {"message": ["Укажите комментарий покупателю."]},
        }},
    ]}
    for error in (RequestPlayerokError(response), RequestFailedError(response)):
        rendered = items_manager_ui._listing_error_detail(error)
        assert "Укажите комментарий покупателю." in rendered
    assert "тайм-аут" in items_manager_ui._listing_error_detail(RequestSendingError("playerok.com", "тайм-аут"))
    listing = SimpleNamespace(status_description="Не заполнены данные товара")
    assert "Не заполнены данные товара" in items_manager_ui._publication_status_detail(listing)
