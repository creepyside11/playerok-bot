from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
import urllib.parse
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

PLUGIN_META = {
    "id": "autosmm",
    "name": "AutoSMM (Накрутка / SMM панель)",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Автоматическое создание заказов в SMM-панелях (smmway и любых сервисах со стандартным SMM API v2) "
        "при покупке лотов на Playerok. Поддерживает привязку нескольких лотов с индивидуальными услугами и количеством, "
        "а также проверку баланса и соединения."
    ),
    "settings": {
        "api_url": {
            "label": "API URL панели",
            "type": "str",
            "default": "https://smmway.com/api/v2",
        },
        "api_key": {
            "label": "API Token / Key",
            "type": "str",
            "default": "",
        },
        "mappings": {
            "label": "Привязка лотов (JSON)",
            "type": "str",
            "default": "{}",
        },
        "default_service_id": {
            "label": "ID услуги по умолчанию",
            "type": "str",
            "default": "",
        },
        "default_quantity": {
            "label": "Количество по умолчанию",
            "type": "int",
            "default": 100,
        },
        "auto_confirm_deal": {
            "label": "Подтверждать сделку в SENT после заказа",
            "type": "bool",
            "default": True,
        },
        "notify_tg": {
            "label": "Уведомлять в TG о создании заказа",
            "type": "bool",
            "default": True,
        },
    },
}

_URL_RE = re.compile(r"https?://[^\s<>\"']+|@[a-zA-Z0-9_]{4,32}|t\.me/[a-zA-Z0-9_]{4,32}|vk\.com/[a-zA-Z0-9_.-]+")


def _smm_request(api_url: str, params: dict[str, Any]) -> dict[str, Any]:
    url = api_url.strip()
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "Playerok-AutoSMM-Bot/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {err.code}: {err_body[:300]}") from err
    except Exception as exc:
        raise RuntimeError(f"Ошибка запроса к SMM API: {exc}") from exc


async def smm_call(api_url: str, params: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_smm_request, api_url, params)


def _parse_mappings(raw: str) -> dict[str, dict[str, Any]]:
    """Парсит настройки связки лотов.
    
    Поддерживает:
    1. JSON:
       {
         "item_id_1": {"service_id": 120, "quantity": 500},
         "item_id_2": {"service_id": 340, "quantity": 1000}
       }
    2. Построчный формат:
       <item_id>:<service_id>:<quantity>
       Пример:
       1f1010d1-xxx:150:1000
    """
    clean = (raw or "").strip()
    if not clean or clean == "{}":
        return {}

    # Попытка разобрать как JSON
    if clean.startswith("{"):
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                normalized = {}
                for k, v in parsed.items():
                    key = str(k).strip()
                    if isinstance(v, dict):
                        srv = v.get("service_id") or v.get("service")
                        qty = v.get("quantity") or v.get("count")
                        normalized[key] = {"service_id": str(srv), "quantity": int(qty or 0)}
                    elif isinstance(v, (int, str)):
                        normalized[key] = {"service_id": str(v), "quantity": 0}
                return normalized
        except Exception:
            pass

    # Попытка разобрать построчный формат: item_id:service_id:quantity
    mappings = {}
    for line in clean.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) >= 3:
            mappings[parts[0].strip()] = {
                "service_id": parts[1].strip(),
                "quantity": int(parts[2].strip()),
            }
        elif len(parts) == 2:
            mappings[parts[0].strip()] = {
                "service_id": parts[1].strip(),
                "quantity": 0,
            }
    return mappings


def _extract_target_link(deal: Any) -> str | None:
    """Пытается извлечь целевую ссылку покупателя из сделки или данных полей."""
    # 1. Проверяем data_fields сделки / комментария покупателя
    fields = getattr(deal, "data_fields", []) or []
    for f in fields:
        val = getattr(f, "value", None) or (f.get("value") if isinstance(f, dict) else None)
        if val:
            match = _URL_RE.search(str(val))
            if match:
                return match.group(0)

    # 2. Проверяем obtaining / comment
    comment = getattr(deal, "comment", None) or getattr(deal, "instruction_for_seller", None)
    if comment:
        match = _URL_RE.search(str(comment))
        if match:
            return match.group(0)

    return None


async def on_deal(ctx: Any, deal: Any) -> None:
    """Обработчик новых сделок Playerok для автоматического заказа в SMM панели."""
    config = ctx.config
    api_key = str(config.get("api_key") or "").strip()
    api_url = str(config.get("api_url") or "https://smmway.com/api/v2").strip()
    if not api_key:
        return

    # Получаем информацию о предмете сделки
    item = getattr(deal, "item", None)
    if not item:
        return

    item_id = str(getattr(item, "id", "") or "").strip()
    item_name = str(getattr(item, "name", "") or getattr(item, "title", "Товар")).strip()
    deal_id = str(getattr(deal, "id", "") or "").strip()

    # Проверяем маппинг лотов
    mappings = _parse_mappings(str(config.get("mappings") or ""))
    service_id = None
    quantity = 0

    if item_id in mappings:
        service_id = mappings[item_id].get("service_id")
        quantity = mappings[item_id].get("quantity", 0)
    else:
        # Проверяем fallback настройки
        default_srv = str(config.get("default_service_id") or "").strip()
        if default_srv:
            service_id = default_srv
            quantity = int(config.get("default_quantity") or 100)

    if not service_id:
        return

    # Извлекаем ссылку покупателя для накрутки
    link = _extract_target_link(deal)
    if not link:
        logger.info("AutoSMM: Ссылка для накрутки не найдена в сделке %s", deal_id)
        if config.get("notify_tg"):
            await ctx.notify(
                f"⚠️ <b>AutoSMM</b>: По сделке <code>{deal_id}</code> ({item_name}) не удалось автоматически найти ссылку покупателя.\n"
                f"Требуется ручное оформление услуги (ID услуги: <code>{service_id}</code>, кол-во: <b>{quantity}</b>)."
            )
        return

    # Нормализуем юзернеймы вида @username -> https://t.me/username если ссылка на tg
    if link.startswith("@"):
        link = f"https://t.me/{link[1:]}"

    # Создаем заказ в SMM панели
    payload = {
        "key": api_key,
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity,
    }

    try:
        response = await smm_call(api_url, payload)
    except Exception as exc:
        logger.exception("AutoSMM request failed for deal %s", deal_id)
        if config.get("notify_tg"):
            await ctx.notify(
                f"❌ <b>AutoSMM: Ошибка создания заказа</b> по сделке <code>{deal_id}</code>:\n"
                f"<code>{str(exc)[:300]}</code>\n"
                f"Товар: {item_name}\nУслуга: {service_id}, Ссылка: {link}, Кол-во: {quantity}"
            )
        return

    if "error" in response:
        err_text = response.get("error")
        logger.error("AutoSMM API error response: %s", err_text)
        if config.get("notify_tg"):
            await ctx.notify(
                f"❌ <b>AutoSMM: Сервис вернул ошибку</b> по сделке <code>{deal_id}</code>:\n"
                f"<b>{err_text}</b>\n"
                f"Товар: {item_name} (Услуга {service_id})"
            )
        return

    order_id = response.get("order")
    logger.info("AutoSMM: успешно создан заказ #%s для сделки %s", order_id, deal_id)

    # Опционально подтверждаем сделку в SENT (товар отправлен)
    if config.get("auto_confirm_deal") and hasattr(ctx.client, "update_deal"):
        try:
            from playerokapi.enums import ItemDealStatuses
            await ctx.client.update_deal(deal_id, ItemDealStatuses.SENT)
        except Exception as exc:
            logger.warning("AutoSMM: не удалось изменить статус сделки %s на SENT: %s", deal_id, exc)

    if config.get("notify_tg"):
        await ctx.notify(
            f"🚀 <b>AutoSMM: Заказ успешно отправлен в работу!</b>\n\n"
            f"📦 Товар: <b>{item_name}</b>\n"
            f"🆔 Сделка: <code>{deal_id}</code>\n"
            f"⚡ SMM Заказ: <code>#{order_id}</code>\n"
            f"🎯 Услуга: <code>{service_id}</code>\n"
            f"🔗 Ссылка: <code>{link}</code>\n"
            f"🔢 Количество: <b>{quantity}</b>"
        )


async def on_action(ctx: Any, action: str, payload: dict[str, Any]) -> None:
    """Хук действий интерфейса: проверка соединения и баланса SMM панели."""
    if action != "test_connection":
        return

    config = ctx.config
    api_key = str(config.get("api_key") or "").strip()
    api_url = str(config.get("api_url") or "https://smmway.com/api/v2").strip()

    if not api_key:
        await ctx.notify("⚠️ <b>AutoSMM</b>: Сначала укажите API Token в настройках плагина.")
        return

    try:
        data = await smm_call(api_url, {"key": api_key, "action": "balance"})
        if "error" in data:
            await ctx.notify(
                f"❌ <b>AutoSMM: Ошибка авторизации</b>\nОтвет API: <code>{data.get('error')}</code>"
            )
            return

        balance = data.get("balance", "—")
        currency = data.get("currency", "RUB")
        mappings = _parse_mappings(str(config.get("mappings") or ""))
        await ctx.notify(
            f"✅ <b>AutoSMM: Соединение успешно установлено!</b>\n\n"
            f"🌐 API: <code>{api_url}</code>\n"
            f"💰 Баланс на сервисе: <b>{balance} {currency}</b>\n"
            f"🔗 Привязано лотов: <b>{len(mappings)}</b>\n\n"
            "<i>Для привязки лотов укажите их ID и параметры услуги в настройке «Привязка лотов (JSON)».</i>"
        )
    except Exception as exc:
        await ctx.notify(
            f"❌ <b>AutoSMM: Не удалось подключиться</b> к SMM-сервису:\n<code>{str(exc)[:400]}</code>"
        )
