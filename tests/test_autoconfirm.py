import pytest
from app.models import PlayerokAccount, fresh_settings
from app.worker import settings_for


def test_settings_for_autoconfirm_modes():
    account = PlayerokAccount(
        settings={
            "auto_confirm": True,
            "auto_confirm_mode": "categories",
            "auto_confirm_items": ["item1", "item2"],
            "auto_confirm_categories": ["cat_tg", "cat_games"],
        }
    )
    cfg = settings_for(account)
    assert cfg["auto_confirm"] is True
    assert cfg["auto_confirm_mode"] == "categories"
    assert cfg["auto_confirm_items"] == ["item1", "item2"]
    assert cfg["auto_confirm_categories"] == ["cat_tg", "cat_games"]


def test_fresh_settings_has_autoconfirm_fields():
    settings = fresh_settings()
    assert "auto_confirm" in settings
    assert settings["auto_confirm_mode"] == "all"
    assert settings["auto_confirm_items"] == []
    assert settings["auto_confirm_categories"] == []
