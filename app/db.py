from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base


def make_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, future=True, pool_pre_ping=True)

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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Auto-migrate: ensure newly added columns exist in existing PostgreSQL / SQLite tables
        try:
            if str(engine.url).startswith("sqlite"):
                await conn.execute(text("ALTER TABLE telegram_users ADD COLUMN web_login VARCHAR(64)"))
                await conn.execute(text("ALTER TABLE telegram_users ADD COLUMN web_password_hash VARCHAR(128)"))
            else:
                await conn.execute(text("ALTER TABLE telegram_users ADD COLUMN IF NOT EXISTS web_login VARCHAR(64)"))
                await conn.execute(text("ALTER TABLE telegram_users ADD COLUMN IF NOT EXISTS web_password_hash VARCHAR(128)"))
        except Exception:
            pass
