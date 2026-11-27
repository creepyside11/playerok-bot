"""Статические проверки вкладки «ИИ-помощник» в Mini App."""
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parent.parent / "app" / "miniapp_static" / "assets"
DESK_JS = ASSETS / "desk.js"
DESK_CSS = ASSETS / "desk.css"

def test_ai_tab_is_registered():
    source = DESK_JS.read_text(encoding="utf-8")
    assert '["ai", "ИИ-помощник"]' in source
    assert 'telegramTab === "ai"' in source
    assert "renderTelegramAi(" in source

def test_ai_form_exposes_required_fields():
    source = DESK_JS.read_text(encoding="utf-8")
    for field in ('name="baseUrl"', 'name="apiToken"', 'name="model"', 'name="basePrompt"', 'name="mode"', 'name="notifyTg"'):
        assert field in source, field
    assert 'id="telegram-ai-form"' in source
    assert 'type="password"' in source, "токен вводится как пароль и не показывается"

def test_ai_endpoint_is_used():
    source = DESK_JS.read_text(encoding="utf-8")
    assert 'path("/telegram-accounts/ai")' in source
    assert '"PATCH"' in source
    assert 'action === "telegramAiSwitch"' in source
    assert 'action === "telegramAiResetToken"' in source

def test_ai_form_has_error_slot():
    source = DESK_JS.read_text(encoding="utf-8")
    start = source.index('id="telegram-ai-form"')
    block = source[start:start + 2500]
    assert 'class="form-error"' in block

def test_styles_for_ai_form_exist():
    css = DESK_CSS.read_text(encoding="utf-8")
    assert ".field select" in css and ".field textarea" in css

@pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен")
def test_desk_js_is_syntactically_valid():
    result = subprocess.run(
        ["node", "--check", str(DESK_JS)], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
