"""Catalog and draft validation for the Mini App listing wizard."""
from __future__ import annotations

import json
import re
from typing import Any

from aiohttp import BodyPartReader, web

from .item_delivery import BUYER_MESSAGE_ERROR, buyer_message_field_id, get_item_data_fields, item_delivery_payload, valid_buyer_message

MAX_PHOTO_SIZE = 10 * 1024 * 1024
MAX_DRAFT_PHOTOS = 10
MAX_DRAFT_BYTES = 30 * 1024 * 1024
MAX_DRAFT_METADATA = 64 * 1024
# The wizard handles ordinary product fields. Never collect secret credentials.
PRIVATE_FIELD = re.compile(
    r"password|passwd|passphrase|token|secret|session|otp|2fa|private.?key|recovery|login|credentials|"
    r"парол|логин|секрет|токен|сесси|код.*(?:вход|подтвержд)|резервн.*код|ключ.*(?:доступ|api)", re.I,
)


def bad(reason: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(reason=reason)


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise bad("Некорректный идентификатор каталога.")
    return value


def named_entry(entry: Any) -> dict[str, str]:
    return {"id": str(entry.id), "name": str(entry.name or "Без названия")}


async def category_methods(client: Any, category_id: str) -> list[Any]:
    methods = {}
    cursor = None
    seen = set()
    for _ in range(20):
        page = await client.call("get_game_category_obtaining_types", category_id, count=24, after_cursor=cursor)
        if page is None:
            raise web.HTTPBadGateway(reason="Playerok не вернул способы получения.")
        for entry in getattr(page, "obtaining_types", None) or []:
            if entry and entry.id:
                methods[str(entry.id)] = entry
        info = getattr(page, "page_info", None)
        next_cursor = getattr(info, "end_cursor", None)
        if not getattr(info, "has_next_page", False) or not next_cursor:
            return list(methods.values())
        if next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
    raise web.HTTPBadGateway(reason="Не удалось загрузить полный список способов получения.")


def option_groups(category: Any) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for entry in getattr(category, "options", None) or []:
        if not entry or not getattr(entry, "field", None):
            continue
        field = str(entry.field)
        kind = str(getattr(getattr(entry, "type", None), "name", getattr(entry, "type", None)) or "SELECTOR")
        limits = getattr(entry, "value_range_limit", None)
        group = groups.setdefault(field, {
            "field": field, "label": str(getattr(entry, "group", None) or field),
            "kind": "range" if kind == "RANGE" else "select",
            "minimum": None, "maximum": None, "multiple": bool(getattr(entry, "multiple", False)), "choices": [],
        })
        if kind == "RANGE":
            group["kind"] = "range"
            for name, attr in (("minimum", "min"), ("maximum", "max")):
                limit = getattr(limits, attr, None)
                if type(limit) in (int, float):
                    group[name] = limit
        elif getattr(entry, "value", None) is not None:
            value = str(entry.value)
            if not any(choice["value"] == value for choice in group["choices"]):
                group["choices"].append({"value": value, "label": str(getattr(entry, "label", None) or value)})
    return list(groups.values())


async def catalog_schema(client: Any, category_id: str, method_id: str | None) -> tuple[dict[str, Any], str | None]:
    category = await client.call("get_game_category", id=category_id)
    if not category or str(getattr(category, "id", "")) != category_id:
        raise web.HTTPNotFound(reason="Категория не найдена.")
    methods = await category_methods(client, category_id)
    by_id = {str(entry.id): entry for entry in methods}
    if method_id is not None and method_id not in by_id:
        raise bad("Этот способ получения недоступен для выбранной категории.")
    schema = {
        **named_entry(category), "methods": [named_entry(entry) for entry in methods],
        "methodId": method_id, "ready": not methods or method_id is not None,
        "options": option_groups(category), "fields": [], "blocked": False,
    }
    if not schema["ready"]:
        return schema, None
    fields = await get_item_data_fields(client, category_id, method_id)
    buyer_field = buyer_message_field_id(fields)
    for field in fields:
        if str(field.id) == buyer_field:
            continue
        if getattr(field, "hidden", None) or PRIVATE_FIELD.search(str(field.label or "")):
            schema["blocked"] |= bool(field.required)
            continue
        schema["fields"].append({
            "id": str(field.id), "label": str(field.label or "Дополнительные данные"), "required": bool(field.required),
        })
    return schema, buyer_field


def validate_attributes(raw: Any, groups: list[dict[str, Any]]) -> dict[str, Any]:
    known = {entry["field"]: entry for entry in groups}
    if not isinstance(raw, dict) or set(raw) - known.keys():
        raise bad("Параметры категории изменились. Выберите их заново.")
    result = {}
    for key, value in raw.items():
        group = known[key]
        if group["kind"] == "range":
            lo, hi = group["minimum"], group["maximum"]
            if (type(value) is not int or abs(value) >= 2**53
                    or lo is not None and value < lo or hi is not None and value > hi):
                raise bad(f"Некорректное значение параметра «{group['label']}».")
        else:
            allowed = {choice["value"] for choice in group["choices"]}
            if group["multiple"]:
                if not isinstance(value, list) or not value or any(not isinstance(v, str) or v not in allowed for v in value):
                    raise bad(f"Выберите допустимые значения параметра «{group['label']}».")
                value = list(dict.fromkeys(value))
            elif not isinstance(value, str) or value not in allowed:
                raise bad(f"Выберите допустимое значение параметра «{group['label']}».")
        result[key] = value
    return result


def validate_fields(raw: Any, fields: list[dict[str, Any]]) -> dict[str, str]:
    known = {entry["id"]: entry for entry in fields}
    if not isinstance(raw, dict) or set(raw) - known.keys():
        raise bad("Поля категории изменились. Проверьте форму.")
    result = {}
    for key, field in known.items():
        value = raw.get(key, "")
        if not isinstance(value, str) or len(value) > 2048 or (field["required"] and not value.strip()):
            raise bad(f"Заполните поле «{field['label']}».")
        if value.strip():
            result[key] = value.strip()
    return result


def validate_draft(body: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    if set(body) - {"categoryId", "methodId", "name", "price", "description", "comment", "options", "fields"}:
        raise bad("Некорректные данные лота.")
    category_id = identifier(body.get("categoryId"))
    method_id = body.get("methodId")
    if method_id is not None:
        method_id = identifier(method_id)
    result = {}
    for key, label, limit in (("name", "Название", 120), ("description", "Описание", 4096)):
        value = body.get(key)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
            raise bad(f"{label} должно содержать от 1 до {limit} символов.")
        result[key] = value.strip()
    price = body.get("price")
    if type(price) is not int or not 1 <= price <= 10_000_000:
        raise bad("Введите целую цену от 1 до 10 000 000 ₽.")
    result["price"] = price
    if not isinstance(body.get("comment"), str) or not valid_buyer_message(body["comment"]):
        raise bad(BUYER_MESSAGE_ERROR)
    return category_id, method_id, result


async def draft_payload(client: Any, body: dict[str, Any]) -> dict[str, Any]:
    category_id, method_id, result = validate_draft(body)
    schema, buyer_field = await catalog_schema(client, category_id, method_id)
    if not schema["ready"]:
        raise bad("Выберите способ получения товара.")
    if schema["blocked"]:
        raise bad("Категория требует закрытые данные, которые этот мастер не запрашивает.")
    attributes = validate_attributes(body.get("options", {}), schema["options"])
    fields = validate_fields(body.get("fields", {}), schema["fields"])
    comment, values = item_delivery_payload(body["comment"], buyer_field, fields)
    return dict(result, game_category_id=category_id, obtaining_type_id=method_id,
                options=attributes, data_fields=values, comment=comment)


def valid_photo(picture: bytes | bytearray) -> bool:
    return (picture.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
            or picture[:4] == b"RIFF" and picture[8:12] == b"WEBP")


async def read_draft_multipart(request: web.Request) -> tuple[dict[str, Any], list[bytes]]:
    if request.content_type != "multipart/form-data":
        raise web.HTTPUnsupportedMediaType(reason="Ожидается форма нового товара.")
    if request.content_length is not None and request.content_length > MAX_DRAFT_BYTES + MAX_DRAFT_METADATA + 64 * 1024:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_DRAFT_BYTES, actual_size=request.content_length)
    reader = await request.multipart()
    data = None
    photos = []
    total = 0
    while (entry := await reader.next()) is not None:
        if not isinstance(entry, BodyPartReader):
            raise bad("Некорректная форма нового лота.")
        if entry.name == "data" and data is None and not entry.filename:
            limit = MAX_DRAFT_METADATA
        elif entry.name == "image" and entry.filename and len(photos) < MAX_DRAFT_PHOTOS:
            limit = MAX_PHOTO_SIZE
        else:
            raise bad("Некорректная форма или слишком много фотографий (максимум 10).")
        contents = bytearray()
        while chunk := await entry.read_chunk(65536):
            contents.extend(chunk)
            total += len(chunk)
            if len(contents) > limit or total > MAX_DRAFT_BYTES + MAX_DRAFT_METADATA:
                raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(contents))
        if entry.name == "data":
            try:
                data = json.loads(contents)
            except (ValueError, UnicodeError) as exc:
                raise bad("Некорректные данные лота.") from exc
            if not isinstance(data, dict):
                raise bad("Некорректные данные лота.")
        else:
            if not valid_photo(contents):
                raise bad("Поддерживаются изображения JPEG, PNG и WebP.")
            photos.append(bytes(contents))
    if data is None:
        raise bad("Заполните данные лота.")
    if sum(map(len, photos)) > MAX_DRAFT_BYTES:
        raise bad("Фотографии должны занимать не более 30 МБ в сумме.")
    return data, photos
