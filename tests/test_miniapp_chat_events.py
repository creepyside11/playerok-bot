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


@pytest.mark.parametrize("text", ["Обычный текст {ITEM_SEND}", "Привет!", "{{UNKNOWN_TAG}}"])
def test_authored_normal_text_is_not_mistaken_for_system_event(text):
    message = chat_message({
        "id": "m", "text": text, "user": {"id": "buyer", "username": "buyer"},
        "deal": {"id": "deal-1"},
    })
    result = message_json(message, "seller")
    assert result["text"] == text
    assert result["systemEvent"] is None
    assert result["item"] is None


@pytest.mark.parametrize(("tag", "title", "verified_title", "tone"), [
    ("{{ITEM_SENT}}", "Передача товара", "Товар передан", "sent"),
    ("{{ITEM_CONFIRMED}}", "Подтверждение сделки", "Сделка завершена", "success"),
    ("{{DEAL_CONFIRMED}}", "Подтверждение сделки", "Сделка завершена", "success"),
    ("{{ITEM_CONFIRMED_BY_BUYER}}", "Подтверждение сделки", "Сделка завершена", "success"),
    ("{ITEM_PAID}", "Сообщение об оплате", "Товар оплачен", "paid"),
])
@pytest.mark.parametrize("actor_metadata", [False, True])
def test_template_with_author_is_recognized_without_forging_verified_status(
    tag, title, verified_title, tone, actor_metadata,
):
    raw = {
        "id": "m", "text": tag, "event": None,
        "user": {"id": "buyer", "username": "buyer"},
        "deal": {"id": "deal-1"},
    }
    if actor_metadata:
        raw["eventByUser"] = {"id": "buyer", "username": "buyer"}
    result = message_json(chat_message(raw), "seller")
    assert result["systemEvent"]["title"] == (verified_title if actor_metadata else title)
    assert result["systemEvent"]["tone"] == (tone if actor_metadata else "neutral")
    assert result["systemEvent"]["verified"] is actor_metadata
    assert result["systemEvent"]["detail"] == ""
    if not actor_metadata:
        assert "Проверьте состояние сделки" in result["systemEvent"]["description"]


@pytest.mark.parametrize(("tag", "status", "title", "tone"), [
    ("{{ITEM_SENT}}", "SENT", "Товар передан", "sent"),
    ("{{ITEM_CONFIRMED}}", "CONFIRMED", "Сделка завершена", "success"),
    ("{{DEAL_CONFIRMED_AUTOMATICALLY}}", "CONFIRMED_AUTOMATICALLY", "Сделка завершена автоматически", "success"),
])
def test_template_with_matching_actual_deal_status_can_show_verified_status(tag, status, title, tone):
    result = message_json(chat_message({
        "id": "m", "text": tag, "user": {"id": "buyer"},
        "deal": {"id": "deal-1", "status": status},
    }), "seller")
    event = result["systemEvent"]
    assert event["source"] == "deal"
    assert event["verified"]
    assert event["title"] == title and event["tone"] == tone


def test_unverified_confirmation_is_not_promoted_when_deal_only_paid():
    result = message_json(chat_message({
        "id": "m", "text": "{{ITEM_CONFIRMED}}", "user": {"id": "buyer"},
        "deal": {"id": "deal-1", "status": "PAID"},
    }), "seller")
    assert result["systemEvent"]["source"] == "template"
    assert not result["systemEvent"]["verified"]


def test_generic_api_event_does_not_override_known_template():
    result = message_json(chat_message({
        "id": "m", "event": "NEW_MESSAGE", "text": "{{ITEM_SENT}}",
        "user": {"id": "seller", "username": "seller"},
    }), "seller")
    assert result["systemEvent"]["code"] == "ITEM_SENT"
    assert not result["systemEvent"]["verified"]


def test_multiple_deal_hints_recover_complete_item_data():
    message = chat_message({
        "id": "m", "text": "{{ITEM_SENT}}",
        "user": {"id": "seller"}, "deal": {"id": "deal-1", "item": {"id": "lot-1"}},
    })
    same = NS(id="deal-1", item=NS(id="lot-1", name="Набор", price=125, attachment=NS(url=COVER)))
    wrong = NS(id="old-deal", item=NS(id="lot-2", name="Чужой", price=99))
    partial = NS(id="deal-1", item=None)
    result = message_json(message, "seller", [wrong, partial, same])
    assert result["item"] == {"id": "lot-1", "name": "Набор", "price": "125", "cover": COVER}


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
      const sent = JSON.parse(process.argv[3]);
      const sentCard = context.bubble(sent);
      assert.match(sentCard, /event-card event-neutral/);
      assert.match(sentCard, /Передача товара/);
      assert.match(sentCard, /Статус из сообщения/);
      assert.ok(!sentCard.includes('ITEM_SENT'));
      assert.match(context.chatPreview(sent), /Шаблон статуса · Передача товара/);
      const confirmed = JSON.parse(process.argv[4]);
      const confirmedCard = context.bubble(confirmed);
      assert.match(confirmedCard, /event-card event-success/);
      assert.match(confirmedCard, /Сделка завершена/);
      assert.ok(!confirmedCard.includes('ITEM_CONFIRMED'));
    """
    result = subprocess.run(
        [
            "node", "-e", script, str(ASSETS / "desk.js"), json.dumps(message_json(purchase(), "seller")),
            json.dumps(message_json(chat_message({
                "id": "sent", "text": "{{ITEM_SENT}}", "user": {"id": "seller"},
            }), "seller")),
            json.dumps(message_json(chat_message({
                "id": "confirmed", "text": "{{ITEM_CONFIRMED}}", "user": {"id": "buyer"},
                "eventByUser": {"id": "buyer"},
            }), "seller")),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
