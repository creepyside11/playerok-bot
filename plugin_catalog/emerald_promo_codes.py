from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from playerokapi.enums import ItemDealDirections

from app.models import ProcessedEvent
from app.telegram_plugin_runtime import EmeraldSellerAPI


PLUGIN_META = {
    "id": "emerald_promo_codes",
    "name": "EmeraldAI промокоды для лотов",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Создаёт промокоды EmeraldAI для заказов по правилам лотов. "
        "Бонусы за отзыв и подтверждение выдаются только вручную кнопкой продавца."
    ),
    "settings": {
        "api_key": {
            "label": "Emerald Seller API key",
            "type": "str",
            "default": "",
        },
        "lot_rules": {
            "label": "Правила лотов JSON",
            "type": "str",
            "default": '{"*":{"type":"tokens","token_amount":10000,"quantity":1,"prefix":"LOT"}}',
        },
        "feedback_bonus_tokens": {
            "label": "Бонус за отзыв, токены",
            "type": "int",
            "default": 0,
        },
        "confirmation_bonus_tokens": {
            "label": "Бонус за подтверждение, токены",
            "type": "int",
            "default": 0,
        },
        "confirmation_window_hours": {
            "label": "Окно подтверждения, часов",
            "type": "int",
            "default": 24,
        },
        "expires_after_days": {
            "label": "Срок промокода, дней",
            "type": "int",
            "default": 30,
        },
    },
}


def parse_lot_rules(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict):
        value = raw
    else:
        value = json.loads(str(raw or "{}"))
    if not isinstance(value, dict):
        raise ValueError("Правила лотов должны быть JSON-объектом")
    result: dict[str, dict[str, Any]] = {}
    for item_id, rule in value.items():
        if not isinstance(rule, dict):
            raise ValueError(f"Правило лота {item_id} должно быть объектом")
        promo_type = str(rule.get("type", "tokens"))
        if promo_type not in {"tokens", "unlimited"}:
            raise ValueError(f"Неверный type для лота {item_id}")
        normalized = dict(rule)
        normalized["type"] = promo_type
        if promo_type == "tokens":
            amount = int(normalized.get("token_amount", 0))
            if not 10_000 <= amount <= 1_000_000_000:
                raise ValueError(f"token_amount лота {item_id} должен быть 10000..1000000000")
            normalized["token_amount"] = amount
        else:
            minutes = int(normalized.get("duration_minutes", 0))
            if not 1 <= minutes <= 43_200:
                raise ValueError(f"duration_minutes лота {item_id} должен быть 1..43200")
            normalized["duration_minutes"] = minutes
        quantity = int(normalized.get("quantity", 1))
        if not 1 <= quantity <= 100:
            raise ValueError(f"quantity лота {item_id} должен быть 1..100")
        normalized["quantity"] = quantity
        normalized["prefix"] = str(normalized.get("prefix", "LOT"))[:10]
        result[str(item_id)] = normalized
    return result


def find_rule(rules: dict[str, dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    return rules.get(str(item_id)) or rules.get("*")


def build_payload(rule: dict[str, Any], expires_after_days: int) -> dict[str, Any]:
    payload = {
        "type": rule["type"],
        "quantity": rule["quantity"],
        "prefix": rule.get("prefix", "LOT"),
    }
    if rule["type"] == "tokens":
        payload["token_amount"] = rule["token_amount"]
    else:
        payload["duration_minutes"] = rule["duration_minutes"]
    if expires_after_days > 0:
        payload["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(days=expires_after_days)
        ).isoformat().replace("+00:00", "Z")
    return payload


def extract_codes(response: dict[str, Any]) -> list[str]:
    rows = response.get("data") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        return []
    return [str(row["code"]) for row in rows if isinstance(row, dict) and row.get("code")]


async def _claim(ctx: Any, key: str) -> bool:
    async with ctx.db() as session:
        existing = await session.scalar(select(ProcessedEvent).where(
            ProcessedEvent.account_id == ctx.account.id,
            ProcessedEvent.kind == "emerald_promo",
            ProcessedEvent.external_id == key,
        ))
        if existing:
            return False
        session.add(ProcessedEvent(
            account_id=ctx.account.id,
            kind="emerald_promo",
            external_id=key,
        ))
        await session.commit()
    return True


async def _release(ctx: Any, key: str) -> None:
    async with ctx.db() as session:
        row = await session.scalar(select(ProcessedEvent).where(
            ProcessedEvent.account_id == ctx.account.id,
            ProcessedEvent.kind == "emerald_promo",
            ProcessedEvent.external_id == key,
        ))
        if row:
            await session.delete(row)
            await session.commit()


def _deal_by_id(deals: list[Any], deal_id: str) -> Any | None:
    return next((deal for deal in deals if str(getattr(deal, "id", "")) == deal_id), None)


async def _load_deal(ctx: Any, deal_id: str) -> Any | None:
    page = await ctx.client.get_deals(direction=ItemDealDirections.OUT, count=100)
    return _deal_by_id(list(getattr(page, "deals", []) or []), deal_id)


async def _create_codes(ctx: Any, payload: dict[str, Any]) -> list[str]:
    if not ctx.external_api:
        raise RuntimeError("В боте не настроен HTTP-клиент плагинов")
    api_key = str(ctx.config.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("Не указан Emerald Seller API key")
    api = EmeraldSellerAPI(api_key, ctx.external_api.client)
    response = await api.create_promo_code(payload)
    codes = extract_codes(response)
    if not codes:
        raise RuntimeError("EmeraldAI не вернул созданные промокоды")
    return codes


async def _send_codes(ctx: Any, deal: Any, codes: list[str], title: str) -> None:
    chat_id = getattr(getattr(deal, "chat", None), "id", None)
    if not chat_id:
        raise RuntimeError("У заказа нет чата покупателя")
    await ctx.send_chat(
        str(chat_id),
        f"{title}\n" + "\n".join(f"<code>{code}</code>" for code in codes),
    )


async def on_deal(ctx: Any, deal: Any) -> None:
    item_id = str(getattr(getattr(deal, "item", None), "id", "") or "")
    rules = parse_lot_rules(ctx.config.get("lot_rules", "{}"))
    rule = find_rule(rules, item_id)
    if not rule:
        return
    deal_id = str(getattr(deal, "id", ""))
    if not deal_id or not await _claim(ctx, f"order:{deal_id}"):
        return
    try:
        codes = await _create_codes(ctx, build_payload(
            rule, int(ctx.config.get("expires_after_days", 30) or 0)
        ))
        await _send_codes(ctx, deal, codes, "Ваш промокод EmeraldAI:")
        await ctx.notify(
            f"✅ EmeraldAI: по заказу {deal_id} создано кодов: {len(codes)}."
        )
    except Exception as exc:
        await _release(ctx, f"order:{deal_id}")
        await ctx.notify(f"⚠️ EmeraldAI по заказу {deal_id}: {str(exc)[:800]}")

    buttons = []
    if int(ctx.config.get("feedback_bonus_tokens", 0) or 0) >= 10_000:
        buttons.append([InlineKeyboardButton(
            text="⭐ Выдать бонус за отзыв",
            callback_data=f"plugin_action:feedback:{deal_id}",
        )])
    if int(ctx.config.get("confirmation_bonus_tokens", 0) or 0) >= 10_000:
        buttons.append([InlineKeyboardButton(
            text="✅ Выдать бонус за подтверждение",
            callback_data=f"plugin_action:confirmation:{deal_id}",
        )])
    if buttons:
        window = int(ctx.config.get("confirmation_window_hours", 24) or 24)
        await ctx.notify(
            f"Ручные бонусы для заказа {deal_id}. Подтверждение заказа и отзыв "
            f"проверьте самостоятельно; окно подтверждения: {window} ч.",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )


async def on_action(ctx: Any, action: str, payload: dict[str, Any]) -> None:
    if action not in {"feedback", "confirmation"}:
        return
    deal_id = str(payload.get("deal_id") or "")
    amount_key = "feedback_bonus_tokens" if action == "feedback" else "confirmation_bonus_tokens"
    amount = int(ctx.config.get(amount_key, 0) or 0)
    if amount < 10_000:
        raise ValueError("Бонус должен быть не меньше 10000 токенов")
    if not await _claim(ctx, f"{action}:{deal_id}"):
        await ctx.notify(f"ℹ️ Бонус по заказу {deal_id} уже выдавался.")
        return
    try:
        deal = await _load_deal(ctx, deal_id)
        if not deal:
            raise RuntimeError("Заказ не найден среди последних сделок")
        payload_data = build_payload(
            {"type": "tokens", "token_amount": amount, "quantity": 1, "prefix": "BONUS"},
            int(ctx.config.get("expires_after_days", 30) or 0),
        )
        codes = await _create_codes(ctx, payload_data)
        await _send_codes(
            ctx,
            deal,
            codes,
            "Спасибо! Ваш бонус EmeraldAI:",
        )
    except Exception:
        await _release(ctx, f"{action}:{deal_id}")
        raise
    await ctx.notify(f"✅ Бонус EmeraldAI отправлен по заказу {deal_id}.")
