import json
import pytest
from unittest.mock import MagicMock

from playerokapi.account import Account


def item_status_badge(status):
    raw = str(getattr(status, "name", status) or "").upper()
    if raw in ("APPROVED", "ACTIVE"):
        return "🟢 На продаже"
    elif raw in ("PENDING", "MODERATION", "ON_MODERATION"):
        return "⏳ На модерации"
    elif raw in ("SOLD", "COMPLETED"):
        return "📦 Продан"
    elif raw in ("PAUSED", "DISABLED", "ARCHIVED"):
        return "⏸ Снят с продажи"
    return f"ℹ️ {raw}"


def test_item_status_badge():
    class DummyStatus:
        def __init__(self, name):
            self.name = name

    assert item_status_badge(DummyStatus("APPROVED")) == "🟢 На продаже"
    assert item_status_badge(DummyStatus("ACTIVE")) == "🟢 На продаже"
    assert item_status_badge(DummyStatus("PENDING")) == "⏳ На модерации"
    assert item_status_badge(DummyStatus("PAUSED")) == "⏸ Снят с продажи"
    assert item_status_badge(DummyStatus("SOLD")) == "📦 Продан"
    assert item_status_badge("UNKNOWN") == "ℹ️ UNKNOWN"


def test_update_item_graphql_payload():
    account = Account.__new__(Account)
    account.base_url = "https://playerok.com"
    account.requests_timeout = 10
    account.user_agent = "TestAgent"

    captured_requests = []

    def mock_request(method, url, headers, payload, files=None):
        captured_requests.append({
            "method": method,
            "url": url,
            "headers": headers,
            "payload": payload,
            "files": files,
        })
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {
                "updateItem": {
                    "__typename": "MyItem",
                    "id": "123",
                    "name": "Новое имя",
                    "price": 500,
                    "description": "Новое описание",
                    "status": "APPROVED",
                    "attachments": [],
                }
            }
        }
        return mock_resp

    account.request = mock_request

    # 1. Обновление только текста и цены (без файлов)
    account.update_item(
        id="item-123",
        name="Новый заголовок лота",
        price=777,
        description="Новое описание лота",
    )

    assert len(captured_requests) == 1
    req1 = captured_requests[0]
    payload1 = req1["payload"]
    assert payload1["operationName"] == "updateItem"
    assert payload1["variables"]["input"]["id"] == "item-123"
    assert payload1["variables"]["input"]["name"] == "Новый заголовок лота"
    assert payload1["variables"]["input"]["price"] == 777
    assert payload1["variables"]["input"]["description"] == "Новое описание лота"

    # 2. Обновление с фото и удалением старых фото
    fake_png = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    account.update_item(
        id="item-456",
        price=1000,
        remove_attachments=["old-att-1", "old-att-2"],
        add_attachments=[fake_png],
    )

    assert len(captured_requests) == 2
    req2 = captured_requests[1]
    # При передаче файлов payload приходит сериализованным через json.dumps
    operations = json.loads(req2["payload"]["operations"])
    map_data = json.loads(req2["payload"]["map"])
    files = req2["files"]

    assert operations["operationName"] == "updateItem"
    assert operations["variables"]["input"]["id"] == "item-456"
    assert operations["variables"]["input"]["price"] == 1000
    assert operations["variables"]["input"]["removedAttachments"] == ["old-att-1", "old-att-2"]
    assert operations["variables"]["addedAttachments"] == [None]

    # Проверяем правильный маппинг вложений Apollo GraphQL
    assert map_data["1"] == ["variables.addedAttachments.0"]
    assert "1" in files
    filename, file_bytes, content_type = files["1"]
    assert content_type == "image/png"
    assert file_bytes == fake_png
