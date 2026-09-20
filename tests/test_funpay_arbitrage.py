import asyncio
import pytest
from plugin_catalog.funpay_arbitrage import (
    PLUGIN_META,
    FeeCalculator,
    AccountValidator,
    MultiGameAccountChecker,
    ImageGeneratorClient,
    AIClient,
)


def test_funpay_arbitrage_meta_v3():
    assert PLUGIN_META["version"] == "3.0.0"
    assert "image_ai_enabled" in PLUGIN_META["settings"]
    assert "image_ai_model" in PLUGIN_META["settings"]
    assert "upselling_enabled" in PLUGIN_META["settings"]
    assert "steam_web_api_key" in PLUGIN_META["settings"]


def test_upselling_pitch_generation():
    client = AIClient("openai_compatible", "", "https://api.openai.com", "gpt-4o", "sys")
    res = asyncio.run(client.generate_upsell_pitch("Аккаунт Brawl Stars 50k", "допродажа гемов"))
    assert "Brawl Stars" in res or "скидк" in res.lower() or "донат" in res.lower()


def test_roblox_checker_format():
    res = asyncio.run(MultiGameAccountChecker.check_roblox("Roblox"))
    assert "checked" in res
    assert "valid" in res


def test_fee_calculator():
    config = {
        "playerok_fee_percent": 20,
        "funpay_fee_percent": 5,
        "category_fees_json": '{"currency": {"po": 15, "fp": 3}}',
    }
    max_buy = FeeCalculator.calculate_max_buy_price(90.0, "accounts", 30.0, config)
    assert max_buy == 40.0
