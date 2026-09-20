from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

try:
    from telethon import TelegramClient
except ImportError:
    TelegramClient = Any  # type: ignore


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


class EmeraldSellerAPI:
    """Client for the documented EmeraldAI seller API."""

    base_url = "https://emeraldai.sbs/seller/v1"

    def __init__(self, api_key: str, client: Any):
        self.api_key = api_key.strip()
        self.client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def create_promo_code(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.base_url}/promo-codes",
            headers=self._headers(),
            json=payload,
        )
        data = response.json()
        if response.status_code >= 400:
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise RuntimeError(message or f"EmeraldAI API error: HTTP {response.status_code}")
        return data
