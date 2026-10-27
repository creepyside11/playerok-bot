from __future__ import annotations

import asyncio
import logging

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from asyncpg.exceptions import CannotConnectNowError, PostgresConnectionError, TooManyConnectionsError

from .models import Base


logger = logging.getLogger(__name__)

# Колонки, добавленные после первого релиза. Миграции должны быть идемпотентными:
# SQLite не поддерживает ADD COLUMN IF NOT EXISTS, поэтому проверяем PRAGMA
# table_info, а PostgreSQL использует IF NOT EXISTS.
MIGRATIONS: dict[str, dict[str, str]] = {
    "telegram_users": {
        "web_login": "VARCHAR(64)",
        "web_password_hash": "VARCHAR(128)",
    },
    "universal_games": {
        "default_delivery_template": "TEXT",
        "extra_field_name": "VARCHAR(64) DEFAULT 'Доп. данные'",
        "enable_mail_codes": "BOOLEAN DEFAULT TRUE",
    },
    "universal_account_lot_rules": {
        "extra_field_name": "VARCHAR(64)",
        "enable_mail_codes": "BOOLEAN DEFAULT TRUE",
    },
}


def make_engine(database_url: str) -> AsyncEngine:
    connect_args = {"timeout": 15.0} if database_url.startswith("postgresql+asyncpg://") else {}
    engine = create_async_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if database_url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    is_sqlite = str(engine.url).startswith("sqlite")
    async with engine.begin() as conn:
        # Импортируем все модели перед create_all, чтобы создались все таблицы
        from . import accounts_manager, emerald_promo_manager, gmail_accounts_manager, telegram_accounts_manager, roblox_vc_manager, supercell_manager, kosell_manager, autosmm_manager  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)

        # Auto-migrate: добавляем недостающие колонки в существующие БД.
        # Каждая таблица/колонка обрабатывается отдельно, чтобы одна ошибка
        # (например, колонка уже существует) не отменяла остальные миграции.
        for table, columns in MIGRATIONS.items():
            try:
                if is_sqlite:
                    rows = (await conn.execute(text(f'PRAGMA table_info("{table}")'))).fetchall()
                    existing = {str(row[1]) for row in rows}
                    for column, ddl in columns.items():
                        if column not in existing:
                            await conn.execute(
                                text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
                            )
                else:
                    for column, ddl in columns.items():
                        await conn.execute(
                            text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {ddl}')
                        )
            except Exception as exc:
                logger.exception("Не удалось применить миграцию таблицы %s", table)
                if _is_transient_db_error(exc):
                    raise


async def init_db_with_retry(
    engine: AsyncEngine,
    attempts: int | None = None,
    initial_delay: float = 2.0,
) -> None:
    """Retry transient database connection failures until the schema is initialized."""
    max_attempts = max(1, int(attempts)) if attempts is not None else None
    delay = max(0.1, float(initial_delay))
    attempt = 0
    while max_attempts is None or attempt < max_attempts:
        attempt += 1
        try:
            await init_db(engine)
            return
        except (
            DBAPIError,
            PoolTimeoutError,
            OSError,
            asyncio.TimeoutError,
            PostgresConnectionError,
            CannotConnectNowError,
            TooManyConnectionsError,
        ) as exc:
            if not _is_transient_db_error(exc):
                raise
            logger.warning(
                "Не удалось подключиться к БД (попытка %s, %s); повтор через %.1f с",
                attempt,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


def _is_transient_db_error(exc: BaseException) -> bool:
    if isinstance(exc, DBAPIError):
        current: BaseException | None = exc.orig
        while current is not None:
            if isinstance(
                current,
                (
                    PoolTimeoutError,
                    OSError,
                    asyncio.TimeoutError,
                    PostgresConnectionError,
                    CannotConnectNowError,
                    TooManyConnectionsError,
                ),
            ):
                return True
            current = current.__cause__
        return False
    return isinstance(
        exc,
        (
            PoolTimeoutError,
            OSError,
            asyncio.TimeoutError,
            PostgresConnectionError,
            CannotConnectNowError,
            TooManyConnectionsError,
        ),
    )
