from __future__ import annotations

import re
from typing import Any

from aiohttp import web
from sqlalchemy import func, select

from .models import PlayerokAccount
from .telegram_accounts_manager import (
    COUNTRY_PRESETS, default_telegram_api_hash, default_telegram_api_id,
    TelegramAccountLotRule, TelegramAccountStock, TelegramAccountsConfig,
    bind_keyword, valid_keyword,
)


def rules_json(rules: Any) -> list[dict[str, Any]]:
    return [
        {"id": row.id, "lotId": None if row.keyword else row.lot_id, "keyword": row.keyword,
         "title": row.lot_title, "country": row.country, "enabled": row.enabled}
        for row in rules
    ]


def config_json(row: TelegramAccountsConfig | None) -> dict[str, Any]:
    return {
        "apiId": row.api_id if row else default_telegram_api_id(),
        "apiHashSet": bool((row.api_hash if row else "") or default_telegram_api_hash()),
        "defaults": not row or (row.api_id, row.api_hash) == (default_telegram_api_id(), default_telegram_api_hash()),
    }


def country_value(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
        raise web.HTTPBadRequest(reason="Укажите страну.")
    return value.strip()


AI_PLUGIN_ID = "ai_assistant"
AI_HOST = re.compile(r"https?://[^\s]{1,290}|[A-Za-z0-9.\-]{1,200}(:\d{1,5})?(/[^\s]{0,80})?")
AI_LIMITS = {"model": 200, "basePrompt": 8000, "apiToken": 512}


def ai_config_json(enabled: bool, config: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": True,
        "enabled": enabled,
        "baseUrl": str(config.get("base_url") or ""),
        "tokenSet": bool(str(config.get("api_key") or "").strip()),
        "model": str(config.get("model") or ""),
        "basePrompt": str(config.get("base_prompt") or ""),
        "mode": str(config.get("mode") or "help_command_only"),
        "modes": list((settings.get("mode") or {}).get("choices") or []),
        "modeLabels": dict((settings.get("mode") or {}).get("choice_labels") or {}),
        "notifyTg": bool(config.get("notify_tg_on_reply")),
        "defaults": {
            "baseUrl": (settings.get("base_url") or {}).get("default") or "",
            "basePrompt": (settings.get("base_prompt") or {}).get("default") or "",
            "model": (settings.get("model") or {}).get("default") or "",
        },
    }


class TelegramPluginSettings:
    def __init__(self, db: Any, plugins: Any = None) -> None:
        self.db = db
        self.plugins = plugins

    async def panel(self, account: PlayerokAccount) -> dict[str, Any]:
        async with self.db() as session:
            inventory = (await session.execute(select(
                TelegramAccountStock.country, TelegramAccountStock.status, func.count(TelegramAccountStock.id),
            ).where(TelegramAccountStock.account_id == account.id).group_by(
                TelegramAccountStock.country, TelegramAccountStock.status,
            ).order_by(TelegramAccountStock.country))).all()
            rules = await session.scalars(select(TelegramAccountLotRule).where(
                TelegramAccountLotRule.account_id == account.id,
            ).order_by(TelegramAccountLotRule.id.desc()))
            config = await session.scalar(select(TelegramAccountsConfig).where(
                TelegramAccountsConfig.account_id == account.id,
            ))
            by_country: dict[str, dict[str, Any]] = {}
            for country, status, quantity in inventory:
                row = by_country.setdefault(country, {"country": country, "available": 0, "issued": 0, "other": 0})
                row[status if status in {"available", "issued"} else "other"] += quantity
            return {
                "stock": list(by_country.values()),
                "rules": rules_json(rules),
                "config": config_json(config),
                "ai": await self.ai_config(account),
                "countries": list(dict.fromkeys([*(name for name, _ in COUNTRY_PRESETS), *by_country])),
            }

    async def ai_config(self, account: PlayerokAccount) -> dict[str, Any]:
        plugin = self.plugins.plugins.get(AI_PLUGIN_ID) if self.plugins else None
        if plugin is None:
            return {"available": False}
        enabled, config = await self.plugins.resolved_state(account.id, plugin)
        return ai_config_json(enabled, config, plugin.settings)

    async def configure_ai(self, account: PlayerokAccount, body: dict[str, Any]) -> dict[str, Any]:
        plugin = self.plugins.plugins.get(AI_PLUGIN_ID) if self.plugins else None
        if plugin is None:
            raise web.HTTPNotFound(reason="Плагин ИИ не установлен. Включите его в разделе «Плагины».")
        allowed = {"enabled", "baseUrl", "apiToken", "model", "basePrompt", "mode", "notifyTg", "resetToken"}
        if not body or set(body) - allowed:
            raise web.HTTPBadRequest(reason="Некорректные настройки ИИ.")

        enabled = body.get("enabled")
        if "enabled" in body and type(enabled) is not bool:
            raise web.HTTPBadRequest(reason="Некорректное значение переключателя ИИ.")

        values: dict[str, Any] = {}
        if "baseUrl" in body:
            base_url = body["baseUrl"]
            if not isinstance(base_url, str) or not AI_HOST.fullmatch(base_url.strip()):
                raise web.HTTPBadRequest(reason="Укажите Base URL вида https://api.anthropic.com")
            values["base_url"] = base_url.strip()
        for field, key in (("apiToken", "api_key"), ("model", "model"), ("basePrompt", "base_prompt")):
            if field not in body:
                continue
            value = body[field]
            if not isinstance(value, str) or len(value.strip()) > AI_LIMITS[field]:
                raise web.HTTPBadRequest(reason="Некорректное значение настройки ИИ.")
            if field == "apiToken" and not value.strip():
                continue  # пустое поле означает «не менять»
            values[key] = value.strip()
        if "mode" in body:
            mode = body["mode"]
            if mode not in ((plugin.settings.get("mode") or {}).get("choices") or []):
                raise web.HTTPBadRequest(reason="Неизвестный режим работы ИИ.")
            values["mode"] = mode
        if "notifyTg" in body:
            if type(body["notifyTg"]) is not bool:
                raise web.HTTPBadRequest(reason="Некорректная настройка уведомлений ИИ.")
            values["notify_tg_on_reply"] = body["notifyTg"]
        if "resetToken" in body:
            if body["resetToken"] is not True or len(body) != 1:
                raise web.HTTPBadRequest(reason="Сброс токена выполняется отдельно.")
            values["api_key"] = ""

        await self.plugins.configure_state(account.id, plugin, enabled=enabled, values=values or None)
        return await self.ai_config(account)

    async def configure(self, account: PlayerokAccount, body: dict[str, Any]) -> dict[str, Any]:
        if not body or set(body) - {"apiId", "apiHash", "reset"}:
            raise web.HTTPBadRequest(reason="Некорректные настройки Telegram API.")
        api_id = body.get("apiId")
        api_hash = body.get("apiHash")
        reset = body.get("reset") is True
        if "reset" in body and (not reset or len(body) != 1):
            raise web.HTTPBadRequest(reason="Сброс выполняется отдельно.")
        if "apiId" in body and (type(api_id) is not int or not 1 <= api_id <= 2**31 - 1):
            raise web.HTTPBadRequest(reason="API ID должен быть положительным целым числом.")
        if "apiHash" in body and (not isinstance(api_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash)):
            raise web.HTTPBadRequest(reason="API Hash должен содержать 32 шестнадцатеричных символа.")
        async with self.db() as session:
            await session.get(PlayerokAccount, account.id, with_for_update=True)
            row = await session.scalar(select(TelegramAccountsConfig).where(
                TelegramAccountsConfig.account_id == account.id,
            ))
            if row is None:
                row = TelegramAccountsConfig(
                    account_id=account.id, api_id=default_telegram_api_id(), api_hash=default_telegram_api_hash(),
                )
                session.add(row)
            if reset:
                row.api_id, row.api_hash = default_telegram_api_id(), default_telegram_api_hash()
            if api_id is not None:
                row.api_id = api_id
            if api_hash is not None:
                row.api_hash = api_hash
            await session.commit()
            return config_json(row)

    async def bind(self, account: PlayerokAccount, item: Any, country: str) -> dict[str, Any]:
        async with self.db() as session:
            await session.get(PlayerokAccount, account.id, with_for_update=True)
            row = await session.scalar(select(TelegramAccountLotRule).where(
                TelegramAccountLotRule.account_id == account.id, TelegramAccountLotRule.lot_id == str(item.id),
            ))
            if row is None:
                row = TelegramAccountLotRule(account_id=account.id, lot_id=str(item.id), enabled=True)
                session.add(row)
            row.lot_title, row.country = str(item.name or "Лот")[:256], country
            await session.commit()
            return rules_json([row])[0]

    async def bind_keyword(self, account: PlayerokAccount, keyword: Any, country: str) -> dict[str, Any]:
        if not valid_keyword(keyword):
            raise web.HTTPBadRequest(reason="Укажите слово или фразу длиной до 128 символов.")
        return rules_json([await bind_keyword(self.db, account, keyword, country)])[0]

    async def update_rule(
        self, account: PlayerokAccount, rule_id: int, body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if body is not None and (
            not body or set(body) - {"enabled", "country"} or ("enabled" in body and type(body["enabled"]) is not bool)
        ):
            raise web.HTTPBadRequest(reason="Некорректные параметры привязки.")
        async with self.db() as session:
            row = await session.scalar(select(TelegramAccountLotRule).where(
                TelegramAccountLotRule.id == rule_id, TelegramAccountLotRule.account_id == account.id,
            ).with_for_update())
            if row is None:
                raise web.HTTPNotFound(reason="Привязка не найдена.")
            if body is None:
                await session.delete(row)
            else:
                if "country" in body:
                    row.country = country_value(body["country"])
                if "enabled" in body:
                    row.enabled = body["enabled"]
            await session.commit()
            return {"ok": True} if body is None else rules_json([row])[0]
