"""Первая инструкция покупателю должна уходить сразу, без лишнего запроса истории."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.models import PlayerokAccount, TelegramUser
from app.telegram_accounts_manager import (
    TelegramAccountDeal, TelegramAccountStock, format_delivery_message,
)
from plugins import telegram_accounts

async def seed(tmp_path, *, intro_attempted_at=None, created_ago=timedelta(0)):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/intro.db")
    await init_db(engine)
    db = make_session_factory(engine)
    async with db() as session:
        session.add(TelegramUser(id=10))
        await session.flush()
        account = PlayerokAccount(
            tg_user_id=10, playerok_user_id="seller", username="seller",
            cookies_encrypted="fixture", user_agent="test",
        )
        session.add(account)
        await session.flush()
        stock = TelegramAccountStock(
            account_id=account.id, country="Страна", phone="+70000000000", session_encrypted="fixture",
        )
        session.add(stock)
        await session.flush()
        record = TelegramAccountDeal(
            account_id=account.id, deal_id="sale", chat_id="buyer-chat", stock_id=stock.id,
            phone=stock.phone, country=stock.country, intro_sent=None,
            intro_attempted_at=intro_attempted_at,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        if created_ago:
            await session.execute(
                TelegramAccountDeal.__table__.update()
                .where(TelegramAccountDeal.id == record.id)
                .values(created_at=datetime.now(timezone.utc) - created_ago)
            )
            await session.commit()
        record_id = record.id
    return engine, db, record_id

def build_ctx(db):
    return NS(
        account=NS(id=1, tg_user_id=10, playerok_user_id="seller"),
        db=db,
        client=NS(send_message=AsyncMock(return_value=NS(id="sent")), call=AsyncMock()),
        notify=AsyncMock(),
    )

@pytest.mark.asyncio
async def test_first_instruction_skips_history_request(tmp_path):
    engine, db, record_id = await seed(tmp_path)
    try:
        ctx = build_ctx(db)
        async with db() as session:
            record = await session.get(TelegramAccountDeal, record_id)
        await telegram_accounts.send_initial_message(ctx, record)

        ctx.client.call.assert_not_awaited()  # первая отправка не читает историю чата
        ctx.client.send_message.assert_awaited_once_with(
            "buyer-chat", format_delivery_message("Страна", "+70000000000"), mark_chat_as_read=False,
        )
        async with db() as session:
            saved = await session.get(TelegramAccountDeal, record_id)
            assert saved.intro_sent is True and saved.intro_error is None
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_retry_after_failure_still_reconciles(tmp_path):
    """Если попытка уже была, история проверяется — но одним запросом."""
    engine, db, record_id = await seed(
        tmp_path, intro_attempted_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    try:
        ctx = build_ctx(db)
        ctx.client.call.return_value = NS(messages=[], page_info=None)
        async with db() as session:
            record = await session.get(TelegramAccountDeal, record_id)
        await telegram_accounts.send_initial_message(ctx, record)

        assert ctx.client.call.await_count == 1
        ctx.client.call.assert_awaited_once_with("get_chat_messages", "buyer-chat", count=24, after_cursor=None)
        ctx.client.send_message.assert_awaited_once()
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_retry_is_not_duplicated_when_message_already_delivered(tmp_path):
    engine, db, record_id = await seed(
        tmp_path, intro_attempted_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    try:
        ctx = build_ctx(db)
        text = format_delivery_message("Страна", "+70000000000")
        ctx.client.call.return_value = NS(
            messages=[NS(text=text, user=NS(id="seller"), deleted_at=None)], page_info=None,
        )
        async with db() as session:
            record = await session.get(TelegramAccountDeal, record_id)
        await telegram_accounts.send_initial_message(ctx, record)

        ctx.client.send_message.assert_not_awaited()
        async with db() as session:
            assert (await session.get(TelegramAccountDeal, record_id)).intro_sent is True
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_legacy_row_without_attempt_is_reconciled(tmp_path):
    """Старая запись (создана давно, без отметки о попытке) проверяется по истории."""
    engine, db, record_id = await seed(tmp_path, created_ago=timedelta(hours=2))
    try:
        ctx = build_ctx(db)
        ctx.client.call.return_value = NS(messages=[], page_info=None)
        async with db() as session:
            record = await session.get(TelegramAccountDeal, record_id)
        await telegram_accounts.send_initial_message(ctx, record)
        assert ctx.client.call.await_count == 1
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_lease_prevents_concurrent_double_send(tmp_path):
    engine, db, record_id = await seed(tmp_path)
    try:
        ctx = build_ctx(db)
        async with db() as session:
            record = await session.get(TelegramAccountDeal, record_id)
        await telegram_accounts.send_initial_message(ctx, record)
        await telegram_accounts.send_initial_message(ctx, record)
        ctx.client.send_message.assert_awaited_once()
    finally:
        await engine.dispose()
