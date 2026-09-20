from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost/playerok"


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    database_url: str = DEFAULT_DATABASE_URL
    poll_interval: float = 6.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        database_url = (
            os.getenv("DATABASE_URL")
            or os.getenv("Database_URL")
            or os.getenv("database_url")
            or DEFAULT_DATABASE_URL
        ).strip() or DEFAULT_DATABASE_URL

        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required")

        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif database_url.startswith("postgresql://") and not database_url.startswith("postgresql+asyncpg://"):
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif database_url.startswith("sqlite://") and not database_url.startswith("sqlite+aiosqlite://"):
            database_url = database_url.replace("sqlite://", "sqlite+aiosqlite://", 1)

        if not database_url.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise RuntimeError(
                "DATABASE_URL must use postgresql+asyncpg:// or sqlite+aiosqlite://"
            )

        try:
            poll_interval = max(3.0, float(os.getenv("POLL_INTERVAL", "6")))
        except ValueError:
            poll_interval = 6.0

        return cls(
            bot_token=bot_token,
            database_url=database_url,
            poll_interval=poll_interval,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
