from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    database_url: str
    poll_interval: float = 6.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")

        # SQLAlchemy async driver notation. Railway/Render often provide
        # postgresql://...; convert it transparently.
        if database_url.startswith("postgres://"):
            database_url = "postgresql+asyncpg://" + database_url[len("postgres://") :]
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+asyncpg://" + database_url[len("postgresql://") :]

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
