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


class TelegramPluginSettings:
    def __init__(self, db: Any) -> None:
        self.db = db

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
                "countries": list(dict.fromkeys([*(name for name, _ in COUNTRY_PRESETS), *by_country])),
            }

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
