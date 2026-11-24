from __future__ import annotations

from typing import Any

from playerokapi.enums import GameCategoryDataFieldTypes
from playerokapi.types import GameCategoryDataField


BUYER_MESSAGE_LABEL = "Сообщение покупателю после покупки"
BUYER_MESSAGE_PROMPT = f"✉️ <b>{BUYER_MESSAGE_LABEL}</b>\n\n"
BUYER_MESSAGE_ERROR = "Введите сообщение покупателю от 2 до 2048 символов. Это обязательное поле."
SKIP_VALUES = {"", "-", "—", "пропуск", "пропустить", "skip"}


async def get_item_data_fields(
    client: Any, category_id: str, obtaining_id: str | None
) -> list[GameCategoryDataField]:
    fields = {}
    cursor = None
    while True:
        page = await client.call(
            "get_game_category_data_fields",
            category_id, obtaining_id, count=24,
            type=GameCategoryDataFieldTypes.ITEM_DATA, after_cursor=cursor,
        )
        for field in page.data_fields:
            if field and field.type == GameCategoryDataFieldTypes.ITEM_DATA:
                fields[field.id] = field
        next_cursor = getattr(page.page_info, "end_cursor", None)
        if not getattr(page.page_info, "has_next_page", False) or not next_cursor or next_cursor == cursor:
            return list(fields.values())
        cursor = next_cursor


def buyer_message_field_id(fields: list[GameCategoryDataField]) -> str | None:
    for field in fields:
        label = str(field.label or "").casefold()
        russian = any(word in label for word in ("сообщение", "текст")) and any(
            word in label for word in ("покупател", "после покупк", "после оплат")
        )
        english = "message" in label and any(word in label for word in ("buyer", "purchase", "payment"))
        if russian or english:
            return str(field.id)
    return None


def valid_buyer_message(text: str) -> bool:
    text = text.strip()
    return 2 <= len(text) <= 2048 and text.casefold() not in SKIP_VALUES


def item_delivery_payload(
    message: str, message_field_id: str | None, fields: dict[str, str]
) -> tuple[str | None, dict[str, str]]:
    if not valid_buyer_message(message):
        raise ValueError(BUYER_MESSAGE_ERROR)
    if message_field_id is None:
        return message.strip(), fields
    return None, {**fields, message_field_id: message.strip()}
