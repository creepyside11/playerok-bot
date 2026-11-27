"""#код: мгновенный повтор, статусы сделки и понятные сообщения покупателю."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.models import PlayerokAccount, TelegramUser
from app.telegram_accounts_manager import (
    TelegramAccountDeal, TelegramAccountStock, TelegramAccountsConfig,
    format_code_message, format_code_repeat_message, format_code_waiting_message,
)
from plugins import telegram_accounts

CHAT = "buyer-chat"
PHONE = "+70000000000"

@pytest.fixture(autouse=True)
def reset_state():
    telegram_accounts._code_calls.clear()
    telegram_accounts._seller_calls.clear()
    yield
    telegram_accounts._code_calls.clear()
    telegram_accounts._seller_calls.clear()

async def seed(tmp_path, *, code_sent=None):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/code.db")
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
        session.add(TelegramAccountsConfig(
            account_id=account.id, api_id=2040, api_hash="hash",
        ))
        stock = TelegramAccountStock(
            account_id=account.id, country="Страна", phone=PHONE, session_encrypted="ciphertext",
        )
        session.add(stock)
        await session.flush()
        session.add(TelegramAccountDeal(
            account_id=account.id, deal_id="sale", chat_id=CHAT, stock_id=stock.id,
            phone=PHONE, country="Страна", intro_sent=True, code_sent=code_sent,
        ))
        await session.commit()
        account_id = account.id
    return engine, db, account_id

def build(db, account_id, fetch):
    ctx = NS(
        account=NS(id=account_id, tg_user_id=10, playerok_user_id="seller"),
        db=db,
        config={},
        client=NS(send_message=AsyncMock(), update_deal=AsyncMock(), call=AsyncMock()),
        send_chat=AsyncMock(),
        notify=AsyncMock(),
        call_seller=AsyncMock(),
    )
    return ctx

def message(text):
    return NS(text=text, user=NS(username="buyer"))

def chat():
    return NS(id=CHAT)

@pytest.fixture(autouse=True)
def patch_cipher(monkeypatch):
    monkeypatch.setattr(
        telegram_accounts, "svc",
        lambda: NS(cipher=NS(decrypt=lambda value: "telethon-session")),
    )

@pytest.mark.asyncio
async def test_first_request_delivers_code_and_completes_deal(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        fetch = AsyncMock(return_value=("12345", "Login code: 12345"))
        monkeypatch.setattr(telegram_accounts, "fetch_latest_code_from_telethon", fetch)
        ctx = build(db, account_id, fetch)
        await telegram_accounts.on_message(ctx, chat(), message("#код"))

        fetch.assert_awaited_once_with("telethon-session", 2040, "hash")
        ctx.send_chat.assert_awaited_once_with(CHAT, format_code_message("12345"))
        ctx.client.update_deal.assert_awaited_once()
        async with db() as session:
            record = await session.scalar(select(TelegramAccountDeal))
            assert record.code_sent == "12345"
            assert record.code_requested is True and record.deal_completed is True
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_repeat_within_window_is_instant_and_skips_telethon(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path, code_sent="12345")
    try:
        fetch = AsyncMock(return_value=("12345", "Login code: 12345"))
        monkeypatch.setattr(telegram_accounts, "fetch_latest_code_from_telethon", fetch)
        ctx = build(db, account_id, fetch)
        await telegram_accounts.on_message(ctx, chat(), message("#код"))
        fetch.reset_mock()
        ctx.send_chat.reset_mock()

        await telegram_accounts.on_message(ctx, chat(), message("#код"))
        fetch.assert_not_awaited()  # повтор в пределах окна не ходит в Telegram
        ctx.send_chat.assert_awaited_once_with(CHAT, format_code_repeat_message("12345"))
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_repeat_after_window_hits_telegram_again(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path, code_sent="12345")
    try:
        fetch = AsyncMock(return_value=("12345", "Login code: 12345"))
        monkeypatch.setattr(telegram_accounts, "fetch_latest_code_from_telethon", fetch)
        monkeypatch.setattr(telegram_accounts, "CODE_MIN_INTERVAL", 0.0)
        ctx = build(db, account_id, fetch)
        await telegram_accounts.on_message(ctx, chat(), message("#код"))
        fetch.reset_mock()
        ctx.send_chat.reset_mock()

        await telegram_accounts.on_message(ctx, chat(), message("#код"))
        fetch.assert_awaited_once()
        ctx.send_chat.assert_awaited_once_with(CHAT, format_code_repeat_message("12345"))
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_missing_code_without_history_shows_waiting(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        fetch = AsyncMock(return_value=(None, "code_not_found"))
        monkeypatch.setattr(telegram_accounts, "fetch_latest_code_from_telethon", fetch)
        ctx = build(db, account_id, fetch)
        await telegram_accounts.on_message(ctx, chat(), message("#код"))
        ctx.send_chat.assert_awaited_once_with(CHAT, format_code_waiting_message(PHONE))
        ctx.client.update_deal.assert_not_awaited()
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_missing_code_with_history_resends_previous(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path, code_sent="54321")
    try:
        fetch = AsyncMock(return_value=(None, "code_not_found"))
        monkeypatch.setattr(telegram_accounts, "fetch_latest_code_from_telethon", fetch)
        ctx = build(db, account_id, fetch)
        await telegram_accounts.on_message(ctx, chat(), message("#код"))
        ctx.send_chat.assert_awaited_once_with(CHAT, format_code_repeat_message("54321"))
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_unknown_chat_is_silent(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        fetch = AsyncMock(return_value=("12345", ""))
        monkeypatch.setattr(telegram_accounts, "fetch_latest_code_from_telethon", fetch)
        ctx = build(db, account_id, fetch)
        await telegram_accounts.on_message(ctx, NS(id="stranger"), message("#код"))
        ctx.send_chat.assert_not_awaited()
        fetch.assert_not_awaited()
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_lookalike_is_ignored(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        fetch = AsyncMock(return_value=("12345", ""))
        monkeypatch.setattr(telegram_accounts, "fetch_latest_code_from_telethon", fetch)
        ctx = build(db, account_id, fetch)
        for text in ("#код123", "код", "#коды"):
            await telegram_accounts.on_message(ctx, chat(), message(text))
        fetch.assert_not_awaited()
        ctx.send_chat.assert_not_awaited()
    finally:
        await engine.dispose()
