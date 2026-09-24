import pytest
from app.ai_plugin_builder import (
    validate_api_base_url,
    validate_model_id,
    validate_plugin_request,
    inspect_generated_source,
    parse_generated_plugin,
)

def test_validate_api_base_url():
    assert validate_api_base_url("https://api.anthropic.com") == "https://api.anthropic.com"
    with pytest.raises(ValueError):
        validate_api_base_url("http://insecure.com")

def test_validate_model_id():
    assert validate_model_id("claude-3-5-sonnet-20241022") == "claude-3-5-sonnet-20241022"

def test_inspect_generated_source():
    valid_source = """
PLUGIN_META = {
    "id": "test_plugin",
    "name": "Test Plugin",
    "version": "1.0.0",
    "author": "Tester",
    "description": "A test plugin description",
    "settings": {}
}

async def on_message(ctx, chat, message):
    pass
"""
    res = inspect_generated_source(valid_source)
    assert res["id"] == "test_plugin"
    assert res["name"] == "Test Plugin"

def test_parse_generated_plugin():
    xml = """
<summary>Плагин для теста</summary>
<plugin_source>
PLUGIN_META = {"id": "x"}
</plugin_source>
"""
    res = parse_generated_plugin(xml)
    assert "PLUGIN_META" in res.source
    assert res.summary == "Плагин для теста"
