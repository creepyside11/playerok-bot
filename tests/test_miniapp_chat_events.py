from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.miniapp import message_json
from playerokapi.parser import chat_message


COVER = "https://cdn.playerok.com/lot.webp"
ASSETS = Path(__file__).resolve().parents[1] / "app" / "miniapp_static" / "assets"


def purchase():
    # The same shape used by the API's ChatMessageItemDeal fragment.
    return chat_message({
        "id": "msg-1", "text": "{{ITEM_PAID}}", "event": "ITEM_PAID",
        "createdAt": "2026-11-28T10:00:00Z",
        "deal": {
            "id": "deal-1", "status": "CONFIRMED",
            "transaction": {"id": "tx-1", "value": 99},
            "item": {
                "__typename": "MyItem", "id": "lot-1", "name": "Набор <подарок>",
                "price": 125.5, "attachments": [{"url": COVER}],
                "comment": "private-delivery-data",
            },
        },
    })


def test_payment_card_has_lot_price_and_cover_not_private_data():
    card = message_json(purchase(), "seller")
    assert card["systemEvent"]["title"] == "Товар оплачен"
    assert card["systemEvent"]["tone"] == "paid"
    assert card["systemEvent"]["detail"] == ""
    assert card["item"] == {
        "id": "lot-1", "name": "Набор <подарок>", "price": "125.5", "cover": COVER,
    }
    assert "private-delivery-data" not in json.dumps(card)


@pytest.mark.parametrize(("tag", "title", "tone"), [
    ("{ITEM_SEND}", "Товар передан", "sent"),
    ("{{ITEM_SENT}}", "Товар передан", "sent"),
    ("{{OFFICIAL_ITEM_PAID}}", "Товар оплачен", "paid"),
    ("{DEAL_CONFIRMED}", "Сделка завершена", "success"),
    ("{DEAL_CONFIRMED_AUTOMATICALLY}", "Сделка завершена автоматически", "success"),
    ("{DEAL_ROLLED_BACK}", "Возврат средств", "danger"),
    ("{DEAL_HAS_PROBLEM}", "Проблема со сделкой", "warning"),
    ("{PROBLEM_RESOLVED}", "Проблема решена", "success"),
    ("{CHAT_STARTED}", "Диалог начался", "neutral"),
    ("{{NEW_SYSTEM_EVENT}}", "Событие Playerok", "neutral"),
])
def test_system_placeholders_have_readable_presentation(tag, title, tone):
    data = message_json(chat_message({"id": "m", "text": tag}), "seller")
    assert data["systemEvent"]["title"] == title
    assert data["systemEvent"]["tone"] == tone
    assert data["systemEvent"]["detail"] == ""


def test_parser_preserves_unknown_event_even_when_actor_present():
    message = chat_message({
        "id": "m", "event": "ITEM_SEND", "text": "{ITEM_SEND}", "user": {"id": "seller"},
        "item": {
            "__typename": "MyItemProfile", "id": "lot", "name": "Подарок", "price": 0,
            "attachment": {"url": COVER},
        },
    })
    card = message_json(message, "seller")
    assert card["systemEvent"]["title"] == "Товар передан"
    assert card["item"]["cover"] == COVER
    assert card["item"]["price"] == "0"


def test_partial_item_can_use_exact_deal_but_not_unrelated_purchase():
    message = chat_message({"id": "m", "event": "ITEM_PAID", "deal": {"id": "deal-1"}})
    lot = NS(id="lot-1", name="Правильный товар", price=100, attachment=NS(url=COVER))
    unrelated = NS(id="other", item=NS(id="lot-2", name="Другой товар", price=200))
    correct = NS(id="deal-1", item=lot)
    assert message_json(message, "seller", [unrelated])["item"] is None
    assert message_json(message, "seller", [unrelated, correct])["item"]["name"] == lot.name


def test_missing_price_invalid_media_and_real_event_details():
    card = message_json(chat_message({
        "id": "m", "event": "NEW_SERVER_EVENT", "text": "Подробности события",
        "item": {"id": "lot", "name": "Лот", "attachment": {"url": "https://example.org/photo.png"}},
    }), "seller")
    assert card["systemEvent"]["detail"] == "Подробности события"
    assert card["item"]["price"] is None
    assert card["item"]["cover"] is None


@pytest.mark.parametrize("text", ["{ITEM_PAID}", "Обычный текст {ITEM_SEND}", "Привет!"])
def test_authored_text_is_never_mistaken_for_system_event(text):
    message = chat_message({
        "id": "m", "text": text, "user": {"id": "buyer", "username": "buyer"},
        "deal": {"id": "deal-1"},
    })
    result = message_json(message, "seller")
    assert result["text"] == text
    assert result["systemEvent"] is None
    assert result["item"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен")
def test_rendered_card_and_chat_preview_hide_service_tags_and_escape_html():
    # Execute the actual rendering helpers, without bootstrapping Telegram.
    script = r"""
      const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
      const source = fs.readFileSync(process.argv[1], 'utf8').split('function thread() {')[0];
      const context = vm.createContext({ document: { querySelector: () => null }, window: {} });
      vm.runInContext(source, context);
      const message = JSON.parse(process.argv[2]);
      const card = context.bubble(message);
      assert.match(card, /event-card event-paid/);
      assert.match(card, /Товар оплачен/);
      assert.match(card, /Набор &lt;подарок&gt;/);
      assert.match(card, /125,5\s*₽/);
      assert.match(card, /cdn\.playerok\.com\/lot\.webp/);
      assert.ok(!card.includes('ITEM_PAID'));
      assert.match(context.chatPreview(message), /Товар оплачен · Набор/);
      assert.ok(!context.chatRows([{ id: 'chat', title: 'Buyer', last: message }]).includes('ITEM_PAID'));
      const missing = {...message, item: null};
      assert.match(context.bubble(missing), /Данные товара недоступны/);
      assert.match(context.bubble({text: 'Обычное сообщение', mine: true}), /bubble mine/);
    """
    result = subprocess.run(
        ["node", "-e", script, str(ASSETS / "desk.js"), json.dumps(message_json(purchase(), "seller"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
