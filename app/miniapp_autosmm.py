"""Account-scoped AutoSMM management shared by the Mini App views."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from .autosmm_manager import (
    AutoSmmConfig, AutoSmmLotRule, AutoSmmOrder, SmmError, bind_service,
    get_configured_client, get_owned_order, normalize_api_url, provider_service, refresh_order,
    request_order_refill, service_refill_allowed, valid_service_id, validate_quantity,
)

PAGE_SIZE = 20


def rule_json(row: AutoSmmLotRule) -> dict[str, Any]:
    return {
        "id": row.id, "keyword": row.keyword, "lotId": None if row.keyword else row.lot_id,
        "title": row.lot_title, "serviceId": row.service_id, "serviceName": row.service_name,
        "quantity": row.quantity, "enabled": row.enabled, "autoSent": row.auto_sent,
    }


def order_json(row: AutoSmmOrder, can_refill: bool | None = None) -> dict[str, Any]:
    return {
        "id": row.id, "dealId": row.deal_id, "orderId": row.smm_order_id,
        "serviceId": row.service_id, "serviceName": row.service_name, "quantity": row.quantity,
        "link": row.link, "status": row.status, "dealCompleted": row.deal_completed,
        "refillId": row.refill_id, "refillStatus": row.refill_status, "canRefill": can_refill,
        "legacy": not bool(row.provider_url),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "refillRequestedAt": row.refill_requested_at.isoformat() if row.refill_requested_at else None,
    }


def service_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("service") or row.get("id") or ""),
        "name": str(row.get("name") or "Услуга")[:256],
        "category": str(row.get("category") or "")[:128],
        "min": str(row["min"]) if row.get("min") is not None else None,
        "max": str(row["max"]) if row.get("max") is not None else None,
        "rate": str(row["rate"]) if row.get("rate") is not None else None,
        "refill": service_refill_allowed(row),
    }


class AutoSmmPanel:
    def __init__(self, db: Any, cipher: Any):
        self.db = db
        self.cipher = cipher

    async def client(self, account_id: Any) -> Any:
        client, _ = await get_configured_client(self.db, self.cipher, account_id)
        if not client:
            raise SmmError("Сначала настройте API панель в Telegram-боте.")
        return client

    async def panel(self, account_id: Any) -> dict[str, Any]:
        async with self.db() as session:
            cfg = await session.scalar(select(AutoSmmConfig).where(AutoSmmConfig.account_id == account_id))
            rules = list(await session.scalars(select(AutoSmmLotRule).where(
                AutoSmmLotRule.account_id == account_id,
            ).order_by(AutoSmmLotRule.id.desc())))
            try:
                url = normalize_api_url(cfg.api_url) if cfg else None
            except SmmError:
                url = None
            return {
                "configured": bool(cfg and cfg.api_key_encrypted and url),
                "apiUrl": url,
                "rules": [rule_json(row) for row in rules],
            }

    async def balance(self, account_id: Any) -> dict[str, str]:
        client = await self.client(account_id)
        result = await client.get_balance()
        return {"balance": str(result.get("balance") or "0")[:64], "currency": str(result.get("currency") or "")[:12]}

    async def orders(self, account_id: Any, offset: int = 0) -> dict[str, Any]:
        async with self.db() as session:
            rows = list(await session.scalars(select(AutoSmmOrder).where(
                AutoSmmOrder.account_id == account_id,
            ).order_by(AutoSmmOrder.id.desc()).offset(offset).limit(PAGE_SIZE + 1)))
            total = await session.scalar(select(func.count(AutoSmmOrder.id)).where(AutoSmmOrder.account_id == account_id))
        return {"items": [order_json(row) for row in rows[:PAGE_SIZE]], "total": total or 0,
                "next": offset + PAGE_SIZE if len(rows) > PAGE_SIZE else None}

    async def catalog(self, account_id: Any, query: str = "", offset: int = 0) -> dict[str, Any]:
        client = await self.client(account_id)
        rows = [service_json(row) for row in await client.get_services()]
        rows = [row for row in rows if valid_service_id(row["id"])]
        query = query.strip().casefold()
        if query:
            rows = [row for row in rows if query in f'{row["id"]} {row["name"]} {row["category"]}'.casefold()]
        return {"items": rows[offset:offset + PAGE_SIZE], "total": len(rows),
                "next": offset + PAGE_SIZE if offset + PAGE_SIZE < len(rows) else None}

    async def bind(self, account_id: Any, body: dict[str, Any], item: Any = None) -> dict[str, Any]:
        if set(body) - {"keyword", "lotId", "serviceId", "quantity"} or not valid_service_id(body.get("serviceId")):
            raise SmmError("Укажите ID услуги и данные привязки.")
        if ("keyword" in body) == ("lotId" in body):
            raise SmmError("Выберите лот или ключевую фразу.")
        client = await self.client(account_id)
        service = provider_service(await client.get_services(), body["serviceId"])
        if service is None:
            raise SmmError("Услуга не найдена в каталоге SMM-панели.")
        row = await bind_service(
            self.db, account_id, service=service, quantity=body.get("quantity"),
            keyword=body.get("keyword"), lot_id=str(item.id) if item else None,
            lot_title=str(item.name or "Лот") if item else "",
        )
        return rule_json(row)

    async def update_rule(self, account_id: Any, rule_id: int, patch: dict[str, Any] | None) -> dict[str, Any]:
        async with self.db() as session:
            rule = await session.get(AutoSmmLotRule, rule_id)
            if not rule or rule.account_id != account_id:
                raise SmmError("Привязка не найдена.")
            service_id = rule.service_id
        if patch is None:
            async with self.db() as session:
                rule = await session.get(AutoSmmLotRule, rule_id, with_for_update=True)
                if not rule or rule.account_id != account_id:
                    raise SmmError("Привязка не найдена.")
                await session.delete(rule)
                await session.commit()
            return {"id": rule_id}
        if not patch or set(patch) - {"quantity", "enabled", "autoSent"} or any(
            type(patch[key]) is not bool for key in ("enabled", "autoSent") if key in patch
        ):
            raise SmmError("Некорректные настройки привязки.")
        quantity = None
        if "quantity" in patch:
            client = await self.client(account_id)
            service = provider_service(await client.get_services(), service_id)
            if not service:
                raise SmmError("Услуга больше недоступна.")
            quantity = validate_quantity(patch["quantity"], service)
        async with self.db() as session:
            rule = await session.get(AutoSmmLotRule, rule_id, with_for_update=True)
            if not rule or rule.account_id != account_id or rule.service_id != service_id:
                raise SmmError("Привязка изменилась. Обновите список.")
            if quantity is not None:
                rule.quantity = quantity
            if "enabled" in patch:
                rule.enabled = patch["enabled"]
            if "autoSent" in patch:
                rule.auto_sent = patch["autoSent"]
            await session.commit()
            return rule_json(rule)

    async def order(self, account_id: Any, order_id: int) -> dict[str, Any]:
        await get_owned_order(self.db, account_id, order_id)
        client = await self.client(account_id)
        order, can_refill = await refresh_order(self.db, account_id, order_id, client)
        return order_json(order, can_refill)

    async def refill(self, account_id: Any, order_id: int, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) - {"confirm", "confirmLegacy"} or body.get("confirm") is not True:
            raise SmmError("Подтвердите запрос refill.")
        await get_owned_order(self.db, account_id, order_id)
        client = await self.client(account_id)
        order = await request_order_refill(
            self.db, account_id, order_id, client, confirm_legacy=body.get("confirmLegacy") is True,
        )
        return order_json(order, False)
