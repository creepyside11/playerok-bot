from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
import telethon
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.types.auth import SentCodeTypeSms

from app import telegram_accounts_ui, telegram_session_file
from app.crypto import SecretCipher
from app.db import init_db, make_engine, make_session_factory
from app.models import PlayerokAccount, TelegramUser
from app.telegram_accounts_manager import TelegramAccountStock
from app.telegram_session_file import SessionFileError


def session_contents(tmp_path, key):
    path = tmp_path / "fixture.session"
    session = SQLiteSession(str(path))
    session.set_dc(2, "127.0.0.1", 9999)
    session.auth_key = AuthKey(key)
    session.save()
    session.close()
    return path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("phone", ["12025550123", None])
async def test_authenticates_file_using_telegram_dc(tmp_path, monkeypatch, phone):
    contents = session_contents(tmp_path, b"f" * 256)
    client = NS(connect=AsyncMock(), get_me=AsyncMock(return_value=NS(phone=phone)), disconnect=AsyncMock())

    def make_client(session, api_id, api_hash, **settings):
        assert (session.server_address, session.port) == ("149.154.167.51", 443)
        assert settings["receive_updates"] is False
        client.session = session
        return client

    monkeypatch.setattr(telegram_session_file, "TelegramClient", make_client)
    if phone:
        verified_phone, encoded = await telegram_session_file.verify_session_file(contents, 123, "fixture")
        assert verified_phone == "+" + phone
        assert StringSession(encoded).auth_key.key == b"f" * 256
    else:
        with pytest.raises(SessionFileError, match="Telegram не вернул номер"):
            await telegram_session_file.verify_session_file(contents, 123, "fixture")
    client.get_me.assert_awaited_once()
    client.disconnect.assert_awaited_once()


def test_rejects_invalid_and_empty_session_files(tmp_path):
    for contents in (b"text", b"SQLite format 3\x00", session_contents(tmp_path, b"")):
        with pytest.raises(SessionFileError):
            telegram_session_file.read_session_file(contents)


@pytest.mark.asyncio
async def test_manual_login_labels_sms_as_sms(monkeypatch):
    seller = NS(id="seller")
    cfg = NS(api_id=123, api_hash="fixture")
    monkeypatch.setattr(telegram_accounts_ui, "active_account", AsyncMock(return_value=seller))
    monkeypatch.setattr(telegram_accounts_ui, "get_or_create_config", AsyncMock(return_value=cfg))
    client = NS(connect=AsyncMock(), send_code_request=AsyncMock(return_value=NS(
        phone_code_hash="test-challenge", type=SentCodeTypeSms(length=5),
    )))
    monkeypatch.setattr(telethon, "TelegramClient", lambda *args: client)
    pending = {}
    monkeypatch.setattr(telegram_accounts_ui, "PENDING_PHONE_AUTHS", pending)
    status = NS(edit_text=AsyncMock())
    message = NS(from_user=NS(id=8), text="+12025550123", answer=AsyncMock(return_value=status))
    state = NS(get_data=AsyncMock(return_value={"stock_account_id": seller.id}), set_state=AsyncMock())
    await telegram_accounts_ui.tgacc_phone_entered(message, state)
    assert "по SMS" in status.edit_text.await_args.args[0]
    assert pending[8]["phone_code_hash"] == "test-challenge"


@pytest.mark.asyncio
async def test_session_file_handler_stores_encrypted_profile_once(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/stock.db")
    await init_db(engine)
    db = make_session_factory(engine)
    try:
        async with db() as session:
            session.add(TelegramUser(id=10))
            await session.flush()
            seller = PlayerokAccount(tg_user_id=10, playerok_user_id="shop", username="shop",
                                     cookies_encrypted="fixture", user_agent="test")
            session.add(seller)
            await session.commit()

        cipher = SecretCipher("test-bot")
        verifier = AsyncMock(return_value=("+12025550123", "sample-encrypted-on-write"))
        monkeypatch.setattr(telegram_accounts_ui, "active_account", AsyncMock(return_value=seller))
        monkeypatch.setattr(telegram_accounts_ui, "get_or_create_config", AsyncMock(return_value=NS(api_id=123, api_hash="fixture")))
        monkeypatch.setattr(telegram_accounts_ui, "verify_session_file", verifier)
        monkeypatch.setattr(telegram_accounts_ui, "svc", lambda: NS(db=db, cipher=cipher))
        status = NS(edit_text=AsyncMock())
        message = NS(
            from_user=NS(id=10), document=NS(file_name="phone.SESSION", file_size=7), answer=AsyncMock(return_value=status),
        )
        state = NS(
            get_data=AsyncMock(return_value={"stock_account_id": str(seller.id), "chosen_country": "🇺🇸 США"}),
            clear=AsyncMock(),
        )
        bot = NS(download=AsyncMock(side_effect=lambda document, destination, **kwargs: destination.write(b"fixture")))

        await telegram_accounts_ui.tgacc_session_file_entered(message, state, bot)
        assert "добавлена на склад" in status.edit_text.await_args.args[0]
        await telegram_accounts_ui.tgacc_session_file_entered(message, state, bot)
        assert "уже есть" in status.edit_text.await_args.args[0]
        verifier.assert_awaited_with(b"fixture", 123, "fixture")
        async with db() as session:
            record = (await session.scalars(select(TelegramAccountStock))).one()
            assert record.phone == "+12025550123" and record.country == "🇺🇸 США"
            assert record.account_id == seller.id and record.status == "available"
            assert record.session_encrypted != "sample-encrypted-on-write"
            assert cipher.decrypt(record.session_encrypted) == "sample-encrypted-on-write"
    finally:
        await engine.dispose()
