"""
Flask Mini App for Playerok BOT.
iOS 27 Liquid Glass gray aesthetic (pure monochrome/gray translucent glassmorphism).
No emojis, ultra-clean typography, full control over:
- Overview & Live Metrics
- Playerok Accounts Management & Switcher
- Deals & Orders (list, inspect, confirm)
- Chats & Messages (list dialogs, messages, send response)
- Auto-delivery rules & Stock keys management
- Auto-reply triggers and phrases
- Auto-confirm toggles, modes and item/category rules
- Notification preferences
- Plugin states & configuration
- AI Builder & Emerald Promo integration

Supports both Telegram WebApp (initData validation) and Web Login / Secret Token.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import urllib.parse
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

# Ensure parent directory is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.crypto import SecretCipher
from app.models import (
    AutoReplyRule,
    Base,
    DeliveryRule,
    DeliveryStock,
    ItemTemplate,
    PlayerokAccount,
    PluginState,
    ProcessedEvent,
    TelegramUser,
)

logger = logging.getLogger("playerok_webapp")

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)

app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.getenv("SESSION_SECRET") or secrets.token_hex(24)


def get_sync_database_url() -> str:
    db_url = (
        os.getenv("DATABASE_URL")
        or os.getenv("Database_URL")
        or os.getenv("database_url")
        or "sqlite:///bot.db"
    ).strip()
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    elif db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    elif db_url.startswith("sqlite+aiosqlite://"):
        db_url = db_url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return db_url


_engine = None
_SessionFactory = None


def get_db_session() -> Session:
    global _engine, _SessionFactory
    if _engine is None:
        db_url = get_sync_database_url()
        _engine = create_engine(db_url, pool_pre_ping=True)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _SessionFactory()


def get_bot_token() -> str:
    return os.getenv("BOT_TOKEN", "").strip()


def get_cipher() -> SecretCipher | None:
    token = get_bot_token()
    if token:
        try:
            return SecretCipher(token)
        except Exception:
            return None
    return None


def verify_telegram_init_data(init_data_raw: str, bot_token: str) -> dict[str, Any] | None:
    """Verifies Telegram WebApp initData string."""
    if not init_data_raw or not bot_token:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data_raw, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calculated_hash, received_hash):
            user_data = parsed.get("user")
            if user_data:
                return json.loads(user_data)
    except Exception as e:
        logger.warning(f"Telegram initData verification error: {e}")
    return None


def get_current_user_id() -> int | None:
    # 1. From session
    if "user_id" in session:
        return int(session["user_id"])

    # 2. From Telegram initData in header or query
    init_data = request.headers.get("X-Telegram-Init-Data") or request.args.get("tgWebAppData")
    if init_data:
        bot_token = get_bot_token()
        tg_user = verify_telegram_init_data(init_data, bot_token)
        if tg_user and "id" in tg_user:
            uid = int(tg_user["id"])
            session["user_id"] = uid
            return uid

    # 3. From Bearer token (base64 encoded JSON {userId: ...})
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        try:
            import base64

            data = json.loads(base64.b64decode(token).decode("utf-8"))
            if "userId" in data:
                return int(data["userId"])
        except Exception:
            pass

    return None


def auth_required(f: Callable) -> Callable:
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any):
        uid = get_current_user_id()
        if not uid:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)

    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        if get_current_user_id():
            return redirect("/")
        return render_template("login.html")

    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    init_data = (data.get("init_data") or "").strip()

    # Telegram Mini App auth flow
    if init_data:
        bot_token = get_bot_token()
        tg_user = verify_telegram_init_data(init_data, bot_token)
        if tg_user and "id" in tg_user:
            uid = int(tg_user["id"])
            session["user_id"] = uid
            with get_db_session() as db:
                db_user = db.get(TelegramUser, uid)
                if not db_user:
                    db_user = TelegramUser(id=uid, web_login=f"user_{uid}")
                    db.add(db_user)
                    db.commit()
            return jsonify({"ok": True, "redirect": "/"})
        return jsonify({"ok": False, "error": "Invalid Telegram authorization payload"}), 400

    # Web login + password flow
    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password required"}), 400

    pwd_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()

    with get_db_session() as db:
        user = db.execute(
            select(TelegramUser).where(TelegramUser.web_login == username)
        ).scalar_one_or_none()

        if not user or user.web_password_hash != pwd_hash:
            return jsonify({"ok": False, "error": "Invalid login credentials"}), 401

        session["user_id"] = user.id
        return jsonify({"ok": True, "redirect": "/"})


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def index():
    uid = get_current_user_id()
    if not uid:
        return redirect("/login")
    return render_template("index.html")


# =====================================================================
# API ENDPOINTS
# =====================================================================


@app.route("/api/me", methods=["GET"])
@auth_required
def api_me():
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user:
            user = TelegramUser(id=uid, web_login=f"user_{uid}")
            db.add(user)
            db.commit()

        accounts = list(
            db.execute(
                select(PlayerokAccount)
                .where(PlayerokAccount.tg_user_id == uid)
                .order_by(PlayerokAccount.created_at.desc())
            ).scalars()
        )

        active_account = None
        if user.active_account_id:
            active_account = db.get(PlayerokAccount, user.active_account_id)
        if not active_account and accounts:
            active_account = accounts[0]
            user.active_account_id = active_account.id
            db.commit()

        return jsonify(
            {
                "ok": True,
                "user": {
                    "id": user.id,
                    "web_login": user.web_login or f"user_{user.id}",
                    "active_account_id": str(user.active_account_id) if user.active_account_id else None,
                },
                "active_account": {
                    "id": str(active_account.id),
                    "username": active_account.username,
                    "playerok_user_id": active_account.playerok_user_id,
                    "email": active_account.email,
                    "settings": active_account.settings or {},
                    "worker_initialized": active_account.worker_initialized,
                    "updated_at": active_account.updated_at.isoformat() if active_account.updated_at else None,
                }
                if active_account
                else None,
                "accounts": [
                    {
                        "id": str(acc.id),
                        "username": acc.username,
                        "playerok_user_id": acc.playerok_user_id,
                        "email": acc.email,
                        "is_active": acc.id == (active_account.id if active_account else None),
                    }
                    for acc in accounts
                ],
            }
        )


@app.route("/api/accounts/switch", methods=["POST"])
@auth_required
def api_switch_account():
    uid = get_current_user_id()
    data = request.get_json(silent=True) or {}
    account_id_str = data.get("account_id")
    if not account_id_str:
        return jsonify({"ok": False, "error": "Missing account_id"}), 400

    import uuid

    try:
        acc_uuid = uuid.UUID(account_id_str)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid account_id"}), 400

    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        acc = db.get(PlayerokAccount, acc_uuid)
        if not user or not acc or acc.tg_user_id != uid:
            return jsonify({"ok": False, "error": "Account not found"}), 404

        user.active_account_id = acc.id
        db.commit()
        return jsonify({"ok": True, "active_account_id": str(acc.id)})


@app.route("/api/dashboard", methods=["GET"])
@auth_required
def api_dashboard_stats():
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": True, "active": False, "stats": {}})

        acc_id = user.active_account_id
        acc = db.get(PlayerokAccount, acc_id)
        if not acc:
            return jsonify({"ok": True, "active": False, "stats": {}})

        # Aggregate metrics from processed_events
        events = list(
            db.execute(
                select(ProcessedEvent).where(ProcessedEvent.account_id == acc_id)
            ).scalars()
        )

        total_deals = sum(1 for e in events if e.kind == "deal")
        confirmed_deals = sum(1 for e in events if e.kind == "deal_status" and "CONFIRMED" in e.external_id)
        paid_deals = sum(1 for e in events if e.kind == "deal_status" and "PAID" in e.external_id)
        reviews = sum(1 for e in events if e.kind == "review")
        messages = sum(1 for e in events if e.kind == "message")
        delivery_actions = sum(1 for e in events if e.kind == "delivery_action")
        autoconfirm_actions = sum(1 for e in events if e.kind == "autoconfirm_action")

        delivery_rules_count = db.execute(
            select(DeliveryRule).where(DeliveryRule.account_id == acc_id)
        ).scalars().all()
        auto_reply_count = db.execute(
            select(AutoReplyRule).where(AutoReplyRule.account_id == acc_id)
        ).scalars().all()
        plugin_states = db.execute(
            select(PluginState).where(PluginState.account_id == acc_id)
        ).scalars().all()

        return jsonify(
            {
                "ok": True,
                "active": True,
                "stats": {
                    "total_deals": total_deals,
                    "confirmed_deals": confirmed_deals,
                    "paid_deals": paid_deals,
                    "reviews": reviews,
                    "messages": messages,
                    "delivery_actions": delivery_actions,
                    "autoconfirm_actions": autoconfirm_actions,
                    "rules_delivery_count": len(delivery_rules_count),
                    "rules_autoreply_count": len(auto_reply_count),
                    "active_plugins_count": sum(1 for p in plugin_states if p.enabled),
                },
                "settings": acc.settings or {},
            }
        )


@app.route("/api/settings", methods=["GET", "POST"])
@auth_required
def api_settings():
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400

        acc = db.get(PlayerokAccount, user.active_account_id)
        if not acc:
            return jsonify({"ok": False, "error": "Account not found"}), 404

        current_settings = dict(acc.settings or {})

        if request.method == "GET":
            return jsonify({"ok": True, "settings": current_settings})

        data = request.get_json(silent=True) or {}

        # Update notifications
        if "notifications" in data and isinstance(data["notifications"], dict):
            current_settings.setdefault("notifications", {})
            current_settings["notifications"].update(data["notifications"])

        # Update auto_confirm
        if "auto_confirm" in data:
            current_settings["auto_confirm"] = bool(data["auto_confirm"])

        if "auto_confirm_mode" in data:
            current_settings["auto_confirm_mode"] = str(data["auto_confirm_mode"])

        if "auto_confirm_items" in data:
            current_settings["auto_confirm_items"] = list(data["auto_confirm_items"])

        if "auto_confirm_categories" in data:
            current_settings["auto_confirm_categories"] = list(data["auto_confirm_categories"])

        if "ai_builder" in data and isinstance(data["ai_builder"], dict):
            current_settings.setdefault("ai_builder", {})
            current_settings["ai_builder"].update(data["ai_builder"])

        acc.settings = current_settings
        db.commit()
        return jsonify({"ok": True, "settings": current_settings})


# =====================================================================
# LIVE PLAYEROK GATEWAY CALLS (DEALS, CHATS, ITEMS, PROFILE)
# =====================================================================


def get_playerok_client_for_account(acc: PlayerokAccount):
    cipher = get_cipher()
    if not cipher:
        raise RuntimeError("Cipher unavailable. BOT_TOKEN not set.")
    from app.playerok import PlayerokGateway

    gateway = PlayerokGateway(cipher)
    import asyncio

    # Run async get_client synchronously for Flask worker
    loop = asyncio.new_event_loop()
    try:
        client = loop.run_until_complete(gateway.get_client(acc))
        return client, loop
    except Exception:
        loop.close()
        raise


@app.route("/api/playerok/profile", methods=["GET"])
@auth_required
def api_playerok_profile():
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        acc = db.get(PlayerokAccount, user.active_account_id)
        if not acc:
            return jsonify({"ok": False, "error": "Account not found"}), 404

        try:
            client, loop = get_playerok_client_for_account(acc)
            try:
                profile = loop.run_until_complete(client.get_profile())
                return jsonify(
                    {
                        "ok": True,
                        "profile": {
                            "id": getattr(profile, "id", None) or acc.playerok_user_id,
                            "username": getattr(profile, "username", "") or acc.username,
                            "balance": getattr(profile, "balance", 0.0),
                            "rating": getattr(profile, "rating", 0.0),
                            "reviews_count": getattr(profile, "reviews_count", 0),
                            "deals_count": getattr(profile, "deals_count", 0),
                        },
                    }
                )
            finally:
                loop.close()
        except Exception as e:
            logger.warning(f"Failed to fetch live profile: {e}")
            return jsonify(
                {
                    "ok": True,
                    "fallback": True,
                    "profile": {
                        "id": acc.playerok_user_id,
                        "username": acc.username,
                        "email": acc.email,
                        "balance": 0.0,
                    },
                }
            )


@app.route("/api/playerok/deals", methods=["GET"])
@auth_required
def api_playerok_deals():
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        acc = db.get(PlayerokAccount, user.active_account_id)
        if not acc:
            return jsonify({"ok": False, "error": "Account not found"}), 404

        status_filter = request.args.get("status")
        try:
            client, loop = get_playerok_client_for_account(acc)
            try:
                deals = loop.run_until_complete(client.get_deals())
                result = []
                for d in deals:
                    d_dict = {
                        "id": str(getattr(d, "id", "")),
                        "status": str(getattr(d, "status", "")),
                        "price": getattr(d, "price", 0),
                        "title": getattr(getattr(d, "item", None), "name", None) or getattr(d, "title", "Order"),
                        "created_at": str(getattr(d, "created_at", "")),
                        "buyer": getattr(getattr(d, "buyer", None), "username", None) or getattr(d, "buyer_name", "Buyer"),
                        "can_confirm": getattr(d, "status", "").upper() in ("PAID", "EXECUTING", "DELIVERED"),
                    }
                    if not status_filter or d_dict["status"].lower() == status_filter.lower():
                        result.append(d_dict)
                return jsonify({"ok": True, "deals": result})
            finally:
                loop.close()
        except Exception as e:
            logger.warning(f"Failed to fetch live deals: {e}")
            return jsonify({"ok": True, "deals": [], "note": "Live API standby"})


@app.route("/api/playerok/deals/confirm", methods=["POST"])
@auth_required
def api_playerok_deal_confirm():
    uid = get_current_user_id()
    data = request.get_json(silent=True) or {}
    deal_id = data.get("deal_id")
    if not deal_id:
        return jsonify({"ok": False, "error": "deal_id required"}), 400

    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        acc = db.get(PlayerokAccount, user.active_account_id)
        if not acc:
            return jsonify({"ok": False, "error": "Account not found"}), 404

        try:
            client, loop = get_playerok_client_for_account(acc)
            try:
                loop.run_until_complete(client.confirm_deal(str(deal_id)))
                return jsonify({"ok": True, "message": f"Deal {deal_id} confirmed successfully"})
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Error confirming deal {deal_id}: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/playerok/chats", methods=["GET"])
@auth_required
def api_playerok_chats():
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        acc = db.get(PlayerokAccount, user.active_account_id)
        if not acc:
            return jsonify({"ok": False, "error": "Account not found"}), 404

        try:
            client, loop = get_playerok_client_for_account(acc)
            try:
                chats = loop.run_until_complete(client.get_chats())
                result = []
                for c in chats:
                    result.append(
                        {
                            "id": str(getattr(c, "id", "")),
                            "user_id": str(getattr(c, "user_id", "")),
                            "username": getattr(c, "username", "Buyer"),
                            "last_message": getattr(c, "last_message", ""),
                            "updated_at": str(getattr(c, "updated_at", "")),
                            "unread": getattr(c, "unread", False),
                        }
                    )
                return jsonify({"ok": True, "chats": result})
            finally:
                loop.close()
        except Exception as e:
            logger.warning(f"Failed to fetch live chats: {e}")
            return jsonify({"ok": True, "chats": [], "note": "No active chats or standby"})


@app.route("/api/playerok/chats/send", methods=["POST"])
@auth_required
def api_playerok_send_message():
    uid = get_current_user_id()
    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id")
    text_content = data.get("text", "").strip()
    if not chat_id or not text_content:
        return jsonify({"ok": False, "error": "chat_id and text required"}), 400

    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        acc = db.get(PlayerokAccount, user.active_account_id)
        if not acc:
            return jsonify({"ok": False, "error": "Account not found"}), 404

        try:
            client, loop = get_playerok_client_for_account(acc)
            try:
                loop.run_until_complete(client.send_message(chat_id=str(chat_id), text=text_content))
                return jsonify({"ok": True, "message": "Message sent"})
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500


# =====================================================================
# AUTO-DELIVERY RULES & STOCK API
# =====================================================================


@app.route("/api/delivery/rules", methods=["GET", "POST"])
@auth_required
def api_delivery_rules():
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        acc_id = user.active_account_id

        if request.method == "GET":
            rules = list(
                db.execute(
                    select(DeliveryRule).where(DeliveryRule.account_id == acc_id)
                ).scalars()
            )
            # Count stock for each rule
            stocks = list(
                db.execute(
                    select(DeliveryStock).where(DeliveryStock.rule_id.in_([r.id for r in rules]))
                ).scalars()
            ) if rules else []

            stock_map: dict[int, dict[str, int]] = {}
            for s in stocks:
                entry = stock_map.setdefault(s.rule_id, {"total": 0, "available": 0})
                entry["total"] += 1
                if not s.used_at:
                    entry["available"] += 1

            result = []
            for r in rules:
                st = stock_map.get(r.id, {"total": 0, "available": 0})
                result.append(
                    {
                        "id": r.id,
                        "item_id": r.item_id,
                        "mode": r.mode,
                        "message_template": r.message_template,
                        "enabled": r.enabled,
                        "stock_total": st["total"],
                        "stock_available": st["available"],
                    }
                )
            return jsonify({"ok": True, "rules": result})

        data = request.get_json(silent=True) or {}
        item_id = str(data.get("item_id", "")).strip()
        mode = str(data.get("mode", "static")).strip()
        template = str(data.get("message_template", "")).strip()
        enabled = bool(data.get("enabled", True))

        if not item_id:
            return jsonify({"ok": False, "error": "item_id required"}), 400

        existing = db.execute(
            select(DeliveryRule).where(
                DeliveryRule.account_id == acc_id, DeliveryRule.item_id == item_id
            )
        ).scalar_one_or_none()

        if existing:
            existing.mode = mode
            existing.message_template = template
            existing.enabled = enabled
        else:
            existing = DeliveryRule(
                account_id=acc_id,
                item_id=item_id,
                mode=mode,
                message_template=template,
                enabled=enabled,
            )
            db.add(existing)

        db.commit()
        return jsonify({"ok": True, "rule_id": existing.id})


@app.route("/api/delivery/rules/<int:rule_id>", methods=["DELETE", "PATCH"])
@auth_required
def api_delivery_rule_manage(rule_id: int):
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        rule = db.get(DeliveryRule, rule_id)
        if not rule or rule.account_id != user.active_account_id:
            return jsonify({"ok": False, "error": "Rule not found"}), 404

        if request.method == "DELETE":
            db.delete(rule)
            db.commit()
            return jsonify({"ok": True})

        data = request.get_json(silent=True) or {}
        if "enabled" in data:
            rule.enabled = bool(data["enabled"])
        if "mode" in data:
            rule.mode = str(data["mode"])
        if "message_template" in data:
            rule.message_template = str(data["message_template"])
        db.commit()
        return jsonify({"ok": True})


@app.route("/api/delivery/rules/<int:rule_id>/stock", methods=["POST"])
@auth_required
def api_delivery_add_stock(rule_id: int):
    uid = get_current_user_id()
    cipher = get_cipher()
    if not cipher:
        return jsonify({"ok": False, "error": "Encryption cipher unavailable"}), 500

    data = request.get_json(silent=True) or {}
    lines_raw = data.get("lines", "")
    lines = [line.strip() for line in lines_raw.splitlines() if line.strip()]

    if not lines:
        return jsonify({"ok": False, "error": "No items provided"}), 400

    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        rule = db.get(DeliveryRule, rule_id)
        if not rule or rule.account_id != user.active_account_id:
            return jsonify({"ok": False, "error": "Rule not found"}), 404

        for l in lines:
            encrypted = cipher.encrypt(l)
            stock_item = DeliveryStock(rule_id=rule.id, payload_encrypted=encrypted)
            db.add(stock_item)
        db.commit()

        # Count total stock
        total = db.execute(
            select(DeliveryStock).where(DeliveryStock.rule_id == rule.id, DeliveryStock.used_at == None)
        ).scalars().all()

        return jsonify({"ok": True, "added": len(lines), "available": len(total)})


# =====================================================================
# AUTO-REPLY RULES API
# =====================================================================


@app.route("/api/autoreply/rules", methods=["GET", "POST"])
@auth_required
def api_autoreply_rules():
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        acc_id = user.active_account_id

        if request.method == "GET":
            rules = list(
                db.execute(
                    select(AutoReplyRule).where(AutoReplyRule.account_id == acc_id)
                ).scalars()
            )
            return jsonify(
                {
                    "ok": True,
                    "rules": [
                        {
                            "id": r.id,
                            "trigger": r.trigger,
                            "response": r.response,
                            "enabled": r.enabled,
                        }
                        for r in rules
                    ],
                }
            )

        data = request.get_json(silent=True) or {}
        trigger = str(data.get("trigger", "")).strip()
        response_text = str(data.get("response", "")).strip()
        enabled = bool(data.get("enabled", True))

        if not trigger or not response_text:
            return jsonify({"ok": False, "error": "Trigger and response required"}), 400

        rule = AutoReplyRule(
            account_id=acc_id,
            trigger=trigger,
            response=response_text,
            enabled=enabled,
        )
        db.add(rule)
        db.commit()
        return jsonify({"ok": True, "id": rule.id})


@app.route("/api/autoreply/rules/<int:rule_id>", methods=["DELETE", "PATCH"])
@auth_required
def api_autoreply_rule_manage(rule_id: int):
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        rule = db.get(AutoReplyRule, rule_id)
        if not rule or rule.account_id != user.active_account_id:
            return jsonify({"ok": False, "error": "Rule not found"}), 404

        if request.method == "DELETE":
            db.delete(rule)
            db.commit()
            return jsonify({"ok": True})

        data = request.get_json(silent=True) or {}
        if "enabled" in data:
            rule.enabled = bool(data["enabled"])
        if "trigger" in data:
            rule.trigger = str(data["trigger"])
        if "response" in data:
            rule.response = str(data["response"])
        db.commit()
        return jsonify({"ok": True})


# =====================================================================
# PLUGINS API
# =====================================================================


@app.route("/api/plugins", methods=["GET", "POST"])
@auth_required
def api_plugins():
    uid = get_current_user_id()
    with get_db_session() as db:
        user = db.get(TelegramUser, uid)
        if not user or not user.active_account_id:
            return jsonify({"ok": False, "error": "No active account"}), 400
        acc_id = user.active_account_id

        # Catalog of available plugins in playerok-bot
        catalog = [
            {
                "id": "emerald_promo",
                "name": "Emerald AI Promo Codes",
                "description": "Автоматическая генерация и выдача промокодов и API-ключей Emerald AI покупателям при оплате товаров.",
            },
            {
                "id": "bot_hosting",
                "name": "Bot Store Creator",
                "description": "Конструктор и продажа готовых Telegram ботов с автосозданием хостинга на сервере.",
            },
            {
                "id": "gmail_seller",
                "name": "Gmail Accounts 2FA",
                "description": "Автоматическая выдача аккаунтов Google/Gmail с генерацией TOTP 2FA кодов подтверждения.",
            },
            {
                "id": "telegram_accounts",
                "name": "Telegram TData & Session Seller",
                "description": "Автоматическая выдача Telegram аккаунтов (TData, Pyrogram, Telethon session).",
            },
            {
                "id": "funpay_arbitrage",
                "name": "FunPay to Playerok Arbitrage",
                "description": "Парсинг лотов FunPay, автоматическая переоценка и выставление на Playerok с описаниями ИИ.",
            },
            {
                "id": "ai_assistant",
                "name": "AI Smart Assistant",
                "description": "Интеллектуальный автоответчик на базе нейросети для общения с покупателями в чате.",
            },
            {
                "id": "autosmm",
                "name": "AutoSMM Boost",
                "description": "Интеграция с SMM сервисами для накрутки и продвижения соцсетей.",
            },
        ]

        states = list(
            db.execute(
                select(PluginState).where(PluginState.account_id == acc_id)
            ).scalars()
        )
        state_map = {s.plugin_id: s for s in states}

        if request.method == "GET":
            result = []
            for item in catalog:
                st = state_map.get(item["id"])
                result.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "description": item["description"],
                        "enabled": bool(st.enabled) if st else False,
                        "config": st.config if st else {},
                    }
                )
            return jsonify({"ok": True, "plugins": result})

        data = request.get_json(silent=True) or {}
        plugin_id = data.get("plugin_id")
        enabled = data.get("enabled")
        config = data.get("config")

        if not plugin_id:
            return jsonify({"ok": False, "error": "plugin_id required"}), 400

        st = state_map.get(plugin_id)
        if not st:
            st = PluginState(account_id=acc_id, plugin_id=plugin_id, enabled=False, config={})
            db.add(st)

        if enabled is not None:
            st.enabled = bool(enabled)
        if config is not None and isinstance(config, dict):
            st.config = config

        db.commit()
        return jsonify({"ok": True, "plugin_id": plugin_id, "enabled": st.enabled, "config": st.config})


if __name__ == "__main__":
    port = int(os.getenv("PORT") or 5000)
    app.run(host="0.0.0.0", port=port, debug=False)
