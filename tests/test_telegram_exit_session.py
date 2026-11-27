"""#выход: подтверждение, проверка устройства покупателя и завершение сессии."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.models import PlayerokAccount, TelegramUser
from app.telegram_accounts_manager import (
    TelegramAccountDeal, TelegramAccountStock, TelegramAccountsConfig,
    format_code_after_revoke_message, format_exit_already_revoked_message,
    format_exit_cancelled_message, format_exit_check_failed_message,
    format_exit_confirm_message, format_exit_done_message,
    format_exit_expired_message, format_exit_no_buyer_session_message,
    format_exit_not_issued_message,
)
from plugins import telegram_accounts

CHAT = "buyer-chat"
PHONE = "+70000000000"
SESSION = "telethon-session"

@pytest.fixture(autouse=True)
def reset_state():
    telegram_accounts._pending_exit.clear()
    telegram_accounts._code_calls.clear()
    telegram_accounts._seller_calls.clear()
    yield
    telegram_accounts._pending_exit.clear()
    telegram_accounts._code_calls.clear()
    telegram_accounts._seller_calls.clear()

@pytest.fixture(autouse=True)
def patch_cipher(monkeypatch):
    monkeypatch.setattr(
        telegram_accounts, "svc",
        lambda: NS(cipher=NS(decrypt=lambda value: SESSION)),
    )

async def seed(tmp_path, *, code_sent="12345", revoked=False):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/exit.db")
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
        session.add(TelegramAccountsConfig(account_id=account.id, api_id=2040, api_hash="hash"))
        stock = TelegramAccountStock(
            account_id=account.id, country="Страна", phone=PHONE, session_encrypted="ciphertext",
        )
        session.add(stock)
        await session.flush()
        session.add(TelegramAccountDeal(
            account_id=account.id, deal_id="sale", chat_id=CHAT, stock_id=stock.id,
            phone=PHONE, country="Страна", intro_sent=True, code_sent=code_sent,
            session_revoked=revoked,
        ))
        await session.commit()
        account_id = account.id
    return engine, db, account_id

def build(db, account_id):
    return NS(
        account=NS(id=account_id, tg_user_id=10, playerok_user_id="seller"),
        db=db,
        config={},
        client=NS(send_message=AsyncMock(), update_deal=AsyncMock(), call=AsyncMock()),
        send_chat=AsyncMock(),
        notify=AsyncMock(),
        call_seller=AsyncMock(),
    )

def message(text):
    return NS(text=text, user=NS(username="buyer"))

def chat():
    return NS(id=CHAT)

def wire(monkeypatch, *, auths=None, logout=True):
    monkeypatch.setattr(
        telegram_accounts, "list_authorizations", AsyncMock(return_value=auths),
    )
    monkeypatch.setattr(
        telegram_accounts, "logout_telethon_session", AsyncMock(return_value=logout),
    )

async def revoked_flag(db):
    async with db() as session:
        return (await session.scalar(select(TelegramAccountDeal))).session_revoked

@pytest.mark.asyncio
async def test_exit_asks_for_confirmation(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}])
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        ctx.send_chat.assert_awaited_once_with(CHAT, format_exit_confirm_message())
        telegram_accounts.logout_telethon_session.assert_not_awaited()
        assert CHAT in telegram_accounts._pending_exit
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_yes_after_confirmation_revokes_session(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}])
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        ctx.send_chat.reset_mock()

        await telegram_accounts.on_message(ctx, chat(), message("#да"))
        telegram_accounts.logout_telethon_session.assert_awaited_once_with(SESSION, 2040, "hash")
        ctx.send_chat.assert_awaited_once_with(CHAT, format_exit_done_message())
        assert await revoked_flag(db) is True
        assert CHAT not in telegram_accounts._pending_exit
        ctx.notify.assert_awaited()
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_no_cancels_without_logout(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}])
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        ctx.send_chat.reset_mock()

        await telegram_accounts.on_message(ctx, chat(), message("#нет"))
        telegram_accounts.logout_telethon_session.assert_not_awaited()
        ctx.send_chat.assert_awaited_once_with(CHAT, format_exit_cancelled_message())
        assert await revoked_flag(db) is False
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_no_buyer_device_blocks_logout(tmp_path, monkeypatch):
    """Покупатель не вошёл — выходить нельзя, иначе код некуда прислать."""
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=[{"current": True}])
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        ctx.send_chat.reset_mock()

        await telegram_accounts.on_message(ctx, chat(), message("#да"))
        telegram_accounts.logout_telethon_session.assert_not_awaited()
        ctx.send_chat.assert_awaited_once_with(CHAT, format_exit_no_buyer_session_message())
        assert await revoked_flag(db) is False
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_unverifiable_device_list_aborts(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=None)
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        ctx.send_chat.reset_mock()

        await telegram_accounts.on_message(ctx, chat(), message("#да"))
        telegram_accounts.logout_telethon_session.assert_not_awaited()
        ctx.send_chat.assert_awaited_once_with(CHAT, format_exit_check_failed_message())
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_failed_logout_keeps_session_and_alerts_seller(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}], logout=False)
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        ctx.send_chat.reset_mock()
        ctx.notify.reset_mock()

        await telegram_accounts.on_message(ctx, chat(), message("#да"))
        assert await revoked_flag(db) is False
        assert "Не удалось завершить сессию" in ctx.send_chat.await_args.args[1]
        ctx.notify.assert_awaited()
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_exit_requires_issued_code(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path, code_sent=None)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}])
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        ctx.send_chat.assert_awaited_once_with(CHAT, format_exit_not_issued_message())
        assert CHAT not in telegram_accounts._pending_exit
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_exit_twice_reports_already_revoked(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path, revoked=True)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}])
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        ctx.send_chat.assert_awaited_once_with(CHAT, format_exit_already_revoked_message())
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_expired_confirmation_is_reported(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}])
        monkeypatch.setattr(telegram_accounts, "EXIT_CONFIRM_TTL", -1.0)
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        ctx.send_chat.reset_mock()

        await telegram_accounts.on_message(ctx, chat(), message("#да"))
        telegram_accounts.logout_telethon_session.assert_not_awaited()
        ctx.send_chat.assert_awaited_once_with(CHAT, format_exit_expired_message())
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_yes_without_pending_is_ignored(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}])
        ctx = build(db, account_id)
        for text in ("#да", "#нет"):
            await telegram_accounts.on_message(ctx, chat(), message(text))
        ctx.send_chat.assert_not_awaited()
        telegram_accounts.logout_telethon_session.assert_not_awaited()
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_code_after_revoke_is_refused(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path, revoked=True)
    try:
        fetch = AsyncMock(return_value=("99999", "code"))
        monkeypatch.setattr(telegram_accounts, "fetch_latest_code_from_telethon", fetch)
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#код"))
        fetch.assert_not_awaited()
        ctx.send_chat.assert_awaited_once_with(CHAT, format_code_after_revoke_message())
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_session_row_is_kept_after_logout(tmp_path, monkeypatch):
    """Строка сессии остаётся в БД (по требованию), но помечается недействительной."""
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}])
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выход"))
        await telegram_accounts.on_message(ctx, chat(), message("#да"))
        async with db() as session:
            stock = await session.scalar(select(TelegramAccountStock))
            record = await session.scalar(select(TelegramAccountDeal))
            assert stock.session_encrypted == "ciphertext"
            assert record.session_revoked is True
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_exit_alias_works(tmp_path, monkeypatch):
    engine, db, account_id = await seed(tmp_path)
    try:
        wire(monkeypatch, auths=[{"current": True}, {"current": False}])
        ctx = build(db, account_id)
        await telegram_accounts.on_message(ctx, chat(), message("#выйти"))
        ctx.send_chat.assert_awaited_once_with(CHAT, format_exit_confirm_message())
    finally:
        await engine.dispose()
