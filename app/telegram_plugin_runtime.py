from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient


@dataclass(slots=True)
class TelegramUserSession:
    api_id: int
    api_hash: str
    session: str
    client: TelegramClient | None = None


class TelethonManager:
    """Optional Telethon runtime for plugins.

    Plugins can request managed Telegram user sessions instead of creating
    clients themselves. Sessions should be stored encrypted by the caller.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, TelegramUserSession] = {}

    def register(self, name: str, api_id: int, api_hash: str, session: str) -> None:
        self.sessions[name] = TelegramUserSession(api_id, api_hash, session)

    async def get_client(self, name: str) -> TelegramClient:
        data = self.sessions[name]
        if data.client is None:
            data.client = TelegramClient(data.session, data.api_id, data.api_hash)
            await data.client.connect()
        return data.client

    async def disconnect_all(self) -> None:
        for data in self.sessions.values():
            if data.client:
                await data.client.disconnect()


class ExternalAPI:
    """Small async HTTP wrapper available for plugins."""

    def __init__(self, client: Any):
        self.client = client

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self.client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.client.post(url, **kwargs)
