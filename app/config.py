from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///playerok.db"
DEFAULT_MINI_APP_URL = "https://playerok-bot.bothost.tech"


def mini_app_url() -> str:
    url = os.getenv("MINI_APP_URL", DEFAULT_MINI_APP_URL).strip()
    if url:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.fragment:
            raise RuntimeError("MINI_APP_URL must be a public HTTPS URL")
    return url


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    database_url: str = DEFAULT_DATABASE_URL
    poll_interval: float = 6.0
    log_level: str = "INFO"
    port: int = 3000
    mini_app_url: str = DEFAULT_MINI_APP_URL

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

        port_raw = os.getenv("PORT", "3000")
        if not port_raw.isdecimal() or not 1 <= int(port_raw) <= 65535:
            raise RuntimeError("PORT must be a number from 1 to 65535")

        return cls(
            bot_token=bot_token,
            database_url=database_url,
            poll_interval=poll_interval,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            port=int(port_raw),
            mini_app_url=mini_app_url(),
        )
