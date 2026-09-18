from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import PlayerokAccount, PluginState
from .telegram_plugin_runtime import ExternalAPI, TelethonManager


logger = logging.getLogger(__name__)
_PLUGIN_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,32}$")


@dataclass(slots=True)
class PluginSpec:
    id: str
    name: str
    version: str
    description: str
    author: str
    settings: dict[str, dict[str, Any]]
    module: ModuleType

    def defaults(self) -> dict[str, Any]:
        return {key: meta.get("default") for key, meta in self.settings.items()}


@dataclass(slots=True)
class PluginContext:
    account: PlayerokAccount
    client: Any
    bot: Any
    db: async_sessionmaker[AsyncSession]
    config: dict[str, Any]
    telethon: TelethonManager | None = None
    external_api: ExternalAPI | None = None

    async def send_chat(self, chat_id: str, text: str) -> Any:
        return await self.client.send_message(str(chat_id), str(text), mark_chat_as_read=True)

    async def notify(self, text: str) -> Any:
        return await self.bot.send_message(self.account.tg_user_id, str(text))


class PluginManager:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], plugin_dir: str | Path = "plugins") -> None:
        self.db = session_factory
        self.plugin_dir = Path(plugin_dir)
        self.plugins: dict[str, PluginSpec] = {}
        self.load_errors: dict[str, str] = {}
        self.telethon = TelethonManager()
        self.external_api: ExternalAPI | None = None

    def set_external_api(self, api: ExternalAPI) -> None:
        self.external_api = api

    def load(self) -> None:
        self.plugins.clear()
        self.load_errors.clear()
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.plugin_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"playerok_plugins.{path.stem}", path)
                if spec is None or spec.loader is None:
                    raise RuntimeError("cannot create module spec")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                meta = getattr(module, "PLUGIN_META", None)
                if not isinstance(meta, dict):
                    raise ValueError("PLUGIN_META must be a dict")
                plugin_id = str(meta.get("id") or path.stem)
                settings = meta.get("settings") or {}
                normalized = {str(k): dict(v or {}) for k, v in settings.items()}
                self.plugins[plugin_id] = PluginSpec(
                    id=plugin_id,
                    name=str(meta.get("name") or plugin_id),
                    version=str(meta.get("version") or "1.0.0"),
                    description=str(meta.get("description") or ""),
                    author=str(meta.get("author") or "unknown"),
                    settings=normalized,
                    module=module,
                )
            except Exception as exc:
                self.load_errors[path.name] = str(exc)
                logger.exception("Failed to load plugin %s", path)

    def ordered(self) -> list[PluginSpec]:
        return sorted(self.plugins.values(), key=lambda x: x.name.casefold())

    def by_index(self, index: int) -> PluginSpec | None:
        items = self.ordered()
        return items[index] if 0 <= index < len(items) else None

    async def resolved_state(self, account_id: Any, plugin: PluginSpec) -> tuple[bool, dict[str, Any]]:
        async with self.db() as session:
            row = await session.scalar(select(PluginState).where(PluginState.account_id == account_id, PluginState.plugin_id == plugin.id))
        config = plugin.defaults()
        if row and isinstance(row.config, dict):
            config.update(row.config)
        return bool(row.enabled) if row else False, config

    async def count_enabled(self, account_id: Any) -> int:
        total = 0
        for plugin in self.ordered():
            enabled, _ = await self.resolved_state(account_id, plugin)
            total += int(enabled)
        return total

    async def dispatch_message(self, account: PlayerokAccount, client: Any, bot: Any, chat: Any, message: Any) -> None:
        await self._dispatch("on_message", account, client, bot, chat, message)

    async def dispatch_deal(self, account: PlayerokAccount, client: Any, bot: Any, deal: Any) -> None:
        await self._dispatch("on_deal", account, client, bot, deal)

    async def _dispatch(self, hook_name: str, account: PlayerokAccount, client: Any, bot: Any, *args: Any) -> None:
        for plugin in self.ordered():
            hook = getattr(plugin.module, hook_name, None)
            if not hook:
                continue
            enabled, config = await self.resolved_state(account.id, plugin)
            if not enabled:
                continue
            ctx = PluginContext(account, client, bot, self.db, config, self.telethon, self.external_api)
            try:
                if inspect.iscoroutinefunction(hook):
                    await hook(ctx, *args)
                else:
                    await asyncio.to_thread(hook, ctx, *args)
            except Exception:
                logger.exception("Plugin %s failed", plugin.id)
