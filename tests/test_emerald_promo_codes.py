from __future__ import annotations

import pytest

from app.telegram_plugin_runtime import EmeraldSellerAPI
from plugin_catalog.emerald_promo_codes import (
    build_payload,
    extract_codes,
    find_rule,
    parse_lot_rules,
)


def test_parse_lot_rules_validates_token_and_unlimited_rules() -> None:
    rules = parse_lot_rules(
        '{"lot-1":{"type":"tokens","token_amount":25000,"quantity":2,"prefix":"SHOP"},'
        '"lot-2":{"type":"unlimited","duration_minutes":60}}'
    )

    assert rules["lot-1"]["token_amount"] == 25000
    assert rules["lot-1"]["quantity"] == 2
    assert rules["lot-2"]["duration_minutes"] == 60
    assert find_rule(rules, "unknown") is None


def test_parse_lot_rules_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="10000"):
        parse_lot_rules('{"lot":{"type":"tokens","token_amount":9999}}')
    with pytest.raises(ValueError, match="43200"):
        parse_lot_rules('{"lot":{"type":"unlimited","duration_minutes":43201}}')


def test_build_payload_and_extract_codes() -> None:
    payload = build_payload(
        {"type": "tokens", "token_amount": 10000, "quantity": 1, "prefix": "LOT"},
        30,
    )

    assert payload["type"] == "tokens"
    assert payload["token_amount"] == 10000
    assert payload["expires_at"].endswith("Z")
    assert extract_codes({"data": [{"code": "LOT-ABC"}, {"id": 2}]}) == ["LOT-ABC"]


@pytest.mark.asyncio
async def test_emerald_seller_api_sends_bearer_request() -> None:
    class Response:
        status_code = 201

        @staticmethod
        def json():
            return {"data": [{"code": "LOT-ABC"}]}

    class Client:
        def __init__(self):
            self.call = None

        async def post(self, url, **kwargs):
            self.call = (url, kwargs)
            return Response()

    client = Client()
    result = await EmeraldSellerAPI("sk-em-seller-test", client).create_promo_code(
        {"type": "tokens", "token_amount": 10000, "quantity": 1}
    )

    assert result["data"][0]["code"] == "LOT-ABC"
    assert client.call[0].endswith("/seller/v1/promo-codes")
    assert client.call[1]["headers"]["Authorization"] == "Bearer sk-em-seller-test"


@pytest.mark.asyncio
async def test_emerald_seller_api_reports_api_error() -> None:
    class Response:
        status_code = 402

        @staticmethod
        def json():
            return {"error": {"message": "Недостаточно токенов"}}

    class Client:
        async def post(self, url, **kwargs):
            return Response()

    with pytest.raises(RuntimeError, match="Недостаточно токенов"):
        await EmeraldSellerAPI("sk-em-seller-test", Client()).create_promo_code({})
