from __future__ import annotations

import asyncio
from contextlib import closing, suppress
import re
import sqlite3

from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.errors import AuthKeyError, AuthKeyNotFound, UnauthorizedError
from telethon.sessions import StringSession


MAX_SESSION_BYTES = 8 * 1024 * 1024
TELEGRAM_PRODUCTION_DC = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


class SessionFileError(ValueError):
    pass


def read_session_file(contents: bytes) -> StringSession:
    if len(contents) > MAX_SESSION_BYTES:
        raise SessionFileError("Файл должен быть не больше 8 МБ.")
    if not contents.startswith(b"SQLite format 3\x00"):
        raise SessionFileError("Нужен SQLite-файл Telethon с расширением .session.")
    try:
        with closing(sqlite3.connect(":memory:")) as db:
            db.deserialize(contents)
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            table = db.execute("PRAGMA table_list('sessions')").fetchone()
            if not table or table[2] != "table":
                raise SessionFileError("В файле нет таблицы сессий Telethon.")
            rows = db.execute("SELECT dc_id, auth_key FROM sessions LIMIT 2").fetchall()
    except sqlite3.DatabaseError as exc:
        raise SessionFileError("SQLite-файл повреждён или его формат не поддерживается.") from exc

    if len(rows) != 1:
        raise SessionFileError("В файле должна быть одна готовая сессия Telethon.")
    dc_id, key = rows[0]
    address = TELEGRAM_PRODUCTION_DC.get(dc_id)
    if not address or not isinstance(key, bytes) or len(key) != 256 or not any(key):
        raise SessionFileError("В этой сессии нет действующих данных для подключения к Telegram.")

    # The database is untrusted input. Connect only to Telegram's known DCs.
    session = StringSession()
    session.set_dc(dc_id, address, 443)
    session.auth_key = AuthKey(key)
    return session


async def verify_session_file(contents: bytes, api_id: int, api_hash: str) -> tuple[str, str]:
    session = await asyncio.to_thread(read_session_file, contents)
    client = TelegramClient(
        session, api_id, api_hash, receive_updates=False, timeout=8, connection_retries=0, request_retries=0,
    )
    try:
        async with asyncio.timeout(25):
            await client.connect()
            me = await client.get_me()
            phone = getattr(me, "phone", None)
            if not phone or not re.fullmatch(r"\+?\d{7,16}", phone):
                raise SessionFileError("Telegram не вернул номер. Проверьте, что сессия авторизована как пользователь.")
            encoded = client.session.save()
            if not encoded:
                raise SessionFileError("Telegram не подтвердил авторизацию сессии.")
            return "+" + phone.lstrip("+"), encoded
    except (AuthKeyError, AuthKeyNotFound, UnauthorizedError) as exc:
        raise SessionFileError("Telegram отклонил сессию. Возьмите действующий файл Telethon.") from exc
    finally:
        with suppress(Exception):
            await client.disconnect()
