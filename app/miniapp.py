from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from playerokapi.enums import ChatTypes, ItemDealDirections, ItemStatuses
from playerokapi.exceptions import (
    BotCheckDetectedException, RequestFailedError, RequestPlayerokError, RequestSendingError, UnauthorizedError,
)
from playerokapi.types import MyItem

from .config import Settings
from .item_delivery import BUYER_MESSAGE_ERROR, ListingFieldsUnavailable, listing_comment_update, valid_buyer_message
from .item_publication import (
    PublicationChanged, PublicationUnconfirmed, amount, can_publish, confirm_publication,
    publication_balance, publication_tariffs, tariff_price,
)
from .miniapp_listings import (
    MAX_PHOTO_SIZE, catalog_schema, draft_payload, named_entry, read_draft_multipart, valid_photo,
)
from .miniapp_telegram import TelegramPluginSettings, country_value
from .models import DEFAULT_SETTINGS, PlayerokAccount, TelegramUser
from .playerok import PlayerokGateway
from .playerok_errors import playerok_error_text
from .plugin_system import PluginManager, PluginSpec
from .withdrawal_history import payout_history
from .withdrawal_request import WithdrawalError, create_withdrawal, withdrawal_methods


logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("miniapp_static")
MAX_AUTH_AGE = 12 * 60 * 60
PAGE_SIZE = 20
PUBLICATION_QUOTE_TTL = 5 * 60
CHAT_TYPES = {kind.name: kind for kind in ChatTypes}
SECRET_KEY = re.compile(r"token|secret|password|passwd|apikey|apihash|cookie|session|proxy|privatekey|goldenkey|accesskey|authorization|credentials")
NOTIFICATIONS = DEFAULT_SETTINGS["notifications"]
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
        "img-src 'self' https://playerok.com https://*.playerok.com; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; object-src 'none'; "
        "frame-ancestors 'self' https://telegram.org https://*.telegram.org"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def validate_telegram_user(init_data: str, token: str, *, now: int | None = None) -> dict[str, Any]:
    if not init_data or len(init_data) > 12_288:
        raise ValueError("Invalid Telegram data")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=24)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Duplicate Telegram fields")

    signature = fields.pop("hash", "")
    if not re.fullmatch(r"[a-f0-9]{64}", signature):
        raise ValueError("Missing Telegram signature")
    signed = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
    expected = hmac.new(secret, signed.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid Telegram signature")

    timestamp = int(fields.get("auth_date", "0"))
    age = (int(time.time()) if now is None else now) - timestamp
    if not -60 <= age <= MAX_AUTH_AGE:
        raise ValueError("Expired Telegram session")
    user = json.loads(fields.get("user", "null"))
    if not isinstance(user, dict) or type(user.get("id")) is not int or not 0 < user["id"] < 2**63:
        raise ValueError("Invalid Telegram user")
    return user


TELEGRAM_USER = getattr(web, "RequestKey", web.AppKey)("miniapp_telegram_user", dict)


def status_name(value: Any) -> str:
    return str(getattr(value, "name", value) or "")


def media_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if (parsed.scheme == "https" and not parsed.username
                and (hostname == "playerok.com" or hostname.endswith(".playerok.com"))):
            return url
    except ValueError:
        pass
    return None


def account_json(account: PlayerokAccount) -> dict[str, str]:
    return {"id": str(account.id), "username": account.username}


# Events can arrive as an enum, a new/unknown API event, or a text placeholder.
# Keep the presentation independent of the current deal status (historical events
# must not change to "completed" just because the deal was completed later).
CHAT_EVENT_TAG = re.compile(r"(?:\{\{([A-Z][A-Z0-9_]*)\}\}|\{([A-Z][A-Z0-9_]*)\})")
CHAT_EVENT_GROUPS = (
    (("ITEM_PAID", "OFFICIAL_ITEM_PAID"), "Товар оплачен", "Оплата подтверждена Playerok.", "paid", "bag", True),
    (("ITEM_SEND", "ITEM_SENT"), "Товар передан", "Продавец отметил товар как переданный.", "sent", "send", True),
    (("DEAL_CONFIRMED", "ITEM_CONFIRM", "ITEM_CONFIRMED", "ITEM_RECEIVED"), "Сделка завершена", "Получение товара подтверждено.", "success", "check", True),
    (("DEAL_CONFIRMED_AUTOMATICALLY", "ITEM_CONFIRMED_AUTOMATICALLY", "ITEM_AUTO_CONFIRMED"), "Сделка завершена автоматически", "Playerok подтвердил сделку.", "success", "check", True),
    (("DEAL_ROLLED_BACK", "ITEM_ROLLED_BACK", "ITEM_REFUND", "ITEM_REFUNDED"), "Возврат средств", "Сделка возвращена.", "danger", "refresh", True),
    (("DEAL_HAS_PROBLEM", "PROBLEM_REPORTED", "ITEM_PROBLEM", "ITEM_HAS_PROBLEM"), "Проблема со сделкой", "По сделке открыто обращение.", "warning", "info", True),
    (("DEAL_PROBLEM_RESOLVED", "PROBLEM_RESOLVED", "ITEM_PROBLEM_RESOLVED"), "Проблема решена", "Обращение по сделке закрыто.", "success", "check", True),
    (("DEAL_CANCELLED", "ITEM_CANCELLED", "ITEM_CANCEL"), "Сделка отменена", "", "danger", "close", True),
    (("DEAL_STATUS_CHANGED",), "Статус сделки изменён", "", "neutral", "refresh", True),
    (("NEW_REVIEW", "DEAL_REVIEW", "ITEM_REVIEW"), "Новый отзыв", "", "neutral", "chat", True),
    (("CHAT_START", "CHAT_STARTED"), "Диалог начался", "", "neutral", "chat", False),
    (("CHAT_FINISH", "CHAT_FINISHED"), "Диалог завершён", "", "neutral", "check", False),
)
CHAT_EVENT_PRESENTATION = {
    code: (title, description, tone, icon, trade)
    for codes, title, description, tone, icon, trade in CHAT_EVENT_GROUPS
    for code in codes
}


def chat_event_json(message: Any) -> dict[str, Any] | None:
    text = (getattr(message, "text", None) or "").strip()
    tag = CHAT_EVENT_TAG.fullmatch(text)
    linked = bool(getattr(message, "deal", None) or getattr(message, "item", None))
    code = status_name(getattr(message, "event", None)) or getattr(message, "raw_event", None)
    # Do not turn a customer's ordinary text into a trusted payment/status card.
    author = getattr(message, "user", None)
    if not code and tag and not (getattr(author, "id", None) or getattr(author, "username", None)):
        code = tag.group(1) or tag.group(2)
    if not code:
        return None
    title, description, tone, icon, trade = CHAT_EVENT_PRESENTATION.get(
        code, ("Событие Playerok", "", "neutral", "info", linked),
    )
    return {
        "code": code, "title": title, "description": description, "tone": tone, "icon": icon,
        "trade": trade or linked, "detail": text if not tag else "",
    }


def chat_item_json(message: Any, chat_deals: Any = ()) -> dict[str, Any] | None:
    """Use only the lot attached to this message or its exact deal ID."""
    deal = getattr(message, "deal", None)
    deal_id = getattr(deal, "id", None)
    matched_deal = next((entry for entry in chat_deals or ()
                         if deal_id and getattr(entry, "id", None) == deal_id), None)
    candidates = [item for item in (
        getattr(deal, "item", None), getattr(message, "item", None), getattr(matched_deal, "item", None),
    ) if item is not None]
    if not candidates:
        return None
    item_id = getattr(candidates[0], "id", None)
    # Never accidentally label an older purchase using a different lot in the chat.
    candidates = [entry for entry in candidates if entry is candidates[0] or (
        item_id and getattr(entry, "id", None) == item_id
    )]
    name = next((entry.name for entry in candidates if getattr(entry, "name", None)), None)
    price = next((value for entry in candidates
                  if (value := amount(getattr(entry, "price", None))) is not None and value >= 0), None)
    cover = next((url for entry in candidates for photo in (
        [getattr(entry, "attachment", None)] + list(getattr(entry, "attachments", None) or [])
    ) if (url := media_url(getattr(photo, "url", None)))), None)
    return {
        "id": str(item_id) if item_id else None, "name": name,
        "price": format(price, "f") if price is not None else None, "cover": cover,
    }


def message_json(message: Any, own_id: str, chat_deals: Any = ()) -> dict[str, Any]:
    author = getattr(message, "user", None)
    media = list(getattr(message, "images", None) or [])
    if getattr(message, "file", None):
        media.append(message.file)
    event = chat_event_json(message)
    return {
        "id": str(message.id),
        "text": message.text or "",
        "author": getattr(author, "username", None),
        "mine": str(getattr(author, "id", "") or "") == own_id,
        "event": status_name(getattr(message, "event", None)),
        "systemEvent": event,
        "item": chat_item_json(message, chat_deals) if event else None,
        "time": getattr(message, "created_at", None),
        "images": list(dict.fromkeys(url for entry in media if (url := media_url(getattr(entry, "url", None))))),
    }


def chat_json(chat: Any, own_id: str) -> dict[str, Any]:
    other = next((user for user in chat.users or [] if user and str(user.id) != own_id), None)
    kind = status_name(chat.type)
    title = {"NOTIFICATIONS": "Уведомления Playerok", "SUPPORT": "Поддержка Playerok"}.get(kind)
    return {
        "id": str(chat.id),
        "title": title or getattr(other, "username", None) or "Диалог",
        "avatar": media_url(getattr(other, "avatar_url", None)),
        "unread": chat.unread_messages_counter or 0,
        "last": message_json(chat.last_message, own_id, getattr(chat, "deals", None)) if chat.last_message else None,
        "canReply": kind != "NOTIFICATIONS" and chat.is_texting_allowed is not False
                    and status_name(getattr(chat, "status", None)) != "CLOSED",
    }


def listing_json(item: Any) -> dict[str, Any]:
    cover = getattr(item, "attachment", None) or next(iter(getattr(item, "attachments", None) or []), None)
    return {
        "id": str(item.id),
        "name": str(item.name or "Товар"),
        "price": item.price,
        "status": status_name(item.status),
        "cover": media_url(getattr(cover, "url", None)),
        "slug": getattr(item, "slug", None),
    }


def listing_details_json(item: Any) -> dict[str, Any]:
    return {
        **listing_json(item),
        "description": getattr(item, "description", "") or "",
        "basePrice": getattr(item, "raw_price", None),
        "editable": getattr(item, "is_editable", None) is not False,
        "canPublish": can_publish(item),
        "photos": [
            {"id": str(photo.id), "url": url}
            for photo in getattr(item, "attachments", None) or []
            if (url := media_url(getattr(photo, "url", None)))
        ],
    }


def balance_json(balance: dict[str, Any]) -> dict[str, str | None]:
    return {
        dest: format(value, "f") if (value := amount(balance.get(source))) is not None else None
        for dest, source in {
            "total": "value", "available": "available", "frozen": "frozen",
            "pending": "pendingIncome", "withdrawable": "withdrawable",
        }.items()
    }


def resource_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise web.HTTPBadRequest(reason="Некорректный ID лота.")
    return value


def deal_json(deal: Any) -> dict[str, Any]:
    item = getattr(deal, "item", None)
    chat = getattr(deal, "chat", None)
    buyer = getattr(deal, "user", None)
    value = getattr(getattr(deal, "transaction", None), "value", None)
    return {
        "id": str(deal.id),
        "item": listing_json(item) if item else None,
        "buyer": getattr(buyer, "username", None),
        "status": status_name(deal.status),
        "time": getattr(deal, "created_at", None),
        "amount": value if value is not None else getattr(item, "price", None),
        "chatId": str(chat.id) if chat and chat.id else None,
    }


def paginate(page: Any, field: str, serialize: Any) -> dict[str, Any]:
    if page is None:
        raise web.HTTPBadGateway(reason="Playerok не вернул данные.")
    info = getattr(page, "page_info", None)
    return {
        "items": [serialize(entry) for entry in (getattr(page, field) or []) if entry],
        "total": getattr(page, "total_count", None),
        "cursor": getattr(info, "end_cursor", None) if getattr(info, "has_next_page", False) else None,
    }


def cursor(request: web.Request) -> str | None:
    value = request.query.get("cursor")
    if value and len(value) > 1500:
        raise web.HTTPBadRequest(reason="Некорректная страница.")
    return value or None


async def read_json(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(reason="Ожидается JSON.")
    try:
        body = await request.json()
    except (ValueError, UnicodeError) as exc:
        raise web.HTTPBadRequest(reason="Некорректный JSON.") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(reason="Некорректные данные.")
    return body


def is_secret(key: str, meta: dict[str, Any]) -> bool:
    return (
        bool(meta.get("secret") or meta.get("sensitive"))
        or meta.get("type") in {"secret", "password", "token"}
        or bool(SECRET_KEY.search(re.sub(r"[^a-z]", "", key.lower())))
    )


def field_type(meta: dict[str, Any]) -> str:
    kind = meta.get("type", "str")
    return kind if kind in {"bool", "int", "float", "choice"} else "str"


def setting_value(meta: dict[str, Any], value: Any) -> Any:
    kind = field_type(meta)
    valid = {
        "bool": type(value) is bool,
        "int": type(value) is int and abs(value) < 2**53,
        "float": type(value) in {int, float} and abs(value) < 2**53,
        "choice": isinstance(value, str) and value in (meta.get("choices") or []),
        "str": isinstance(value, str) and len(value) <= 8192,
    }[kind]
    if not valid:
        raise web.HTTPBadRequest(reason="Неверное значение настройки.")
    return value


class MiniApp:
    def __init__(
        self, db: async_sessionmaker[AsyncSession], gateway: PlayerokGateway,
        plugins: PluginManager, token: str, bot_profile: Any = None,
    ) -> None:
        self.db = db
        self.gateway = gateway
        self.plugins = plugins
        self.token = token
        self.bot_profile = bot_profile
        self.telegram_plugins = TelegramPluginSettings(db, plugins)
        self.withdrawal_locks: dict[str, asyncio.Lock] = {}
        self.withdrawal_cooldowns: dict[str, float] = {}
        self.listing_locks: dict[str, asyncio.Lock] = {}
        self.publication_quotes: dict[tuple[str, str], dict[str, Any]] = {}

    @web.middleware
    async def middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if not request.path.startswith("/api/"):
            return await handler(request)
        try:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("tma "):
                raise web.HTTPUnauthorized(reason="Откройте приложение из Telegram.")
            try:
                request[TELEGRAM_USER] = validate_telegram_user(auth[4:], self.token)
            except (ValueError, TypeError):
                raise web.HTTPUnauthorized(reason="Сессия Telegram истекла. Откройте приложение заново.") from None
            return await handler(request)
        except web.HTTPException as exc:
            return web.json_response({"error": exc.reason}, status=exc.status)
        except (BotCheckDetectedException, UnauthorizedError, RequestFailedError, RequestPlayerokError, RequestSendingError) as exc:
            return web.json_response({"error": playerok_error_text(exc)}, status=502)
        except Exception as exc:
            logger.warning("Mini App %s failed: %s", request.match_info.route.name, type(exc).__name__)
            return web.json_response({"error": "Playerok временно не вернул данные. Попробуйте обновить экран."}, status=502)

    @staticmethod
    async def prepare(request: web.Request, response: web.StreamResponse) -> None:
        response.headers.update(SECURITY_HEADERS)
        if request.path.startswith("/assets/"):
            cache = "no-cache" if request.path.endswith((".css", ".js")) else "public, max-age=86400"
        else:
            cache = "no-store"
        response.headers["Cache-Control"] = cache

    @staticmethod
    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    @staticmethod
    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def account(self, request: web.Request) -> PlayerokAccount:
        try:
            account_id = uuid.UUID(request.match_info["account_id"])
        except ValueError:
            raise web.HTTPNotFound(reason="Аккаунт не найден.") from None
        async with self.db() as session:
            account = await session.scalar(select(PlayerokAccount).where(
                PlayerokAccount.id == account_id,
                PlayerokAccount.tg_user_id == request[TELEGRAM_USER]["id"],
            ))
        if account is None:
            raise web.HTTPNotFound(reason="Аккаунт не найден.")
        return account

    async def bootstrap(self, request: web.Request) -> web.Response:
        identity = request[TELEGRAM_USER]
        async with self.db() as session:
            user = await session.get(TelegramUser, identity["id"])
            accounts = list(await session.scalars(select(PlayerokAccount).where(
                PlayerokAccount.tg_user_id == identity["id"]
            ).order_by(PlayerokAccount.created_at)))
        active = next((row for row in accounts if user and row.id == user.active_account_id), None)
        active = active or next(iter(accounts), None)
        bot_name = None
        if self.bot_profile is not None:
            try:
                bot_name = getattr(await self.bot_profile(), "username", None)
            except Exception:
                pass
        return web.json_response({
            "name": str(identity.get("first_name") or "Продавец")[:80],
            "botUsername": bot_name,
            "accounts": [account_json(row) for row in accounts],
            "activeAccountId": str(active.id) if active else None,
        })

    async def select_account(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        async with self.db() as session:
            user = await session.get(TelegramUser, account.tg_user_id)
            user.active_account_id = account.id
            await session.commit()
        return web.json_response(account_json(account))

    async def overview(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        balance, lots, deals, enabled = await asyncio.gather(
            client.balance(),
            client.call("get_my_items", statuses=[ItemStatuses.APPROVED], count=1),
            client.get_deals(direction=ItemDealDirections.OUT, count=5),
            self.plugins.count_enabled(account.id),
            return_exceptions=True,
        )
        details = balance_json(balance) if isinstance(balance, dict) else None
        return web.json_response({
            "balance": details["available"] if details else None,
            "balanceDetails": details,
            "unread": getattr(getattr(client, "account", None), "unread_chats_counter", None),
            "lots": getattr(lots, "total_count", None),
            "plugins": enabled if isinstance(enabled, int) else None,
            "deals": [deal_json(deal) for deal in getattr(deals, "deals", []) or [] if deal],
            "partial": any(isinstance(result, Exception) for result in (balance, lots, deals, enabled)),
        })

    async def balance(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        return web.json_response(balance_json(await client.balance()))

    async def withdrawals(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        page_cursor = cursor(request)
        client = await self.gateway.get_client(account)
        return web.json_response(await payout_history(
            client, account.playerok_user_id, after_cursor=page_cursor,
        ))

    async def withdrawal_options(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        methods = await withdrawal_methods(client)
        balance = balance_json(await client.balance())
        probe = getattr(client, "funds_protection", None)
        return web.json_response({
            **methods,
            "withdrawable": balance.get("withdrawable"),
            "fundsProtection": bool(await probe()) if callable(probe) else False,
        })

    async def create_payout(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        body = await read_json(request)
        if body.get("confirm") is not True:
            raise web.HTTPBadRequest(reason="Подтвердите выплату перед отправкой.")
        provider = body.get("provider")
        requisites = body.get("account")
        value = body.get("value")
        method = body.get("paymentMethodId")
        bank = body.get("sbpBankMemberId")
        if not isinstance(provider, str) or not re.fullmatch(r"[A-Z_]{2,32}", provider):
            raise web.HTTPBadRequest(reason="Выберите способ вывода.")
        if not isinstance(requisites, str) or not 1 <= len(requisites) <= 64:
            raise web.HTTPBadRequest(reason="Укажите реквизиты выплаты.")
        if type(value) is not int or not 0 < value < 100_000_000:
            raise web.HTTPBadRequest(reason="Введите сумму целыми рублями.")
        if method is not None and (not isinstance(method, str) or not re.fullmatch(r"[A-Z_]{2,32}", method)):
            raise web.HTTPBadRequest(reason="Неизвестный способ оплаты.")
        if bank is not None and (not isinstance(bank, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", bank)):
            raise web.HTTPBadRequest(reason="Неизвестный банк СБП.")

        key = str(account.id)
        lock = self.withdrawal_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if time.monotonic() - self.withdrawal_cooldowns.get(key, 0) < 3:
                raise web.HTTPConflict(reason="Выплата только что создана. Проверьте историю.")
            client = await self.gateway.get_client(account)
            try:
                result = await create_withdrawal(
                    client, account.playerok_user_id,
                    provider_id=provider, account=requisites, value=value,
                    payment_method_id=method, sbp_bank_member_id=bank,
                )
            except WithdrawalError as exc:
                raise web.HTTPBadRequest(reason=str(exc)) from exc
            self.withdrawal_cooldowns[key] = time.monotonic()
        return web.json_response(result, status=201)

    async def chats(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        kind = request.query.get("type")
        if kind and kind not in CHAT_TYPES:
            raise web.HTTPBadRequest(reason="Неизвестный тип чата.")
        client = await self.gateway.get_client(account)
        page = await client.get_chats(type=CHAT_TYPES.get(kind), count=PAGE_SIZE, after_cursor=cursor(request))
        return web.json_response(paginate(page, "chats", lambda chat: chat_json(chat, account.playerok_user_id)))

    @staticmethod
    async def get_owned_chat(client: Any, own_id: str, chat_id: str) -> Any:
        chat = await client.call("get_chat", chat_id)
        participants = {str(user.id) for user in (getattr(chat, "users", None) or []) if user}
        if chat is None or (status_name(chat.type) == "PM" and own_id not in participants):
            raise web.HTTPNotFound(reason="Чат не найден.")
        return chat

    async def history(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        chat = await self.get_owned_chat(client, account.playerok_user_id, request.match_info["chat_id"])
        page = await client.call("get_chat_messages", chat.id, count=PAGE_SIZE, after_cursor=cursor(request))
        result = paginate(page, "messages", lambda entry: message_json(
            entry, account.playerok_user_id, getattr(chat, "deals", None),
        ))
        return web.json_response({"chat": chat_json(chat, account.playerok_user_id), **result})

    async def reply(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        body = await read_json(request)
        text = body.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise web.HTTPBadRequest(reason="Введите сообщение от 1 до 2000 символов.")
        client = await self.gateway.get_client(account)
        chat = await self.get_owned_chat(client, account.playerok_user_id, request.match_info["chat_id"])
        if not chat_json(chat, account.playerok_user_id)["canReply"]:
            raise web.HTTPForbidden(reason="Playerok не разрешает ответить в этот чат.")
        message = await client.send_message(chat.id, text.strip(), mark_chat_as_read=False)
        if not message:
            raise web.HTTPBadGateway(reason="Playerok не подтвердил отправку. Обновите чат перед повтором.")
        return web.json_response(message_json(message, account.playerok_user_id), status=201)

    async def read_chat(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        chat = await self.get_owned_chat(client, account.playerok_user_id, request.match_info["chat_id"])
        await client.call("mark_chat_as_read", chat.id)
        return web.json_response({"ok": True})

    async def deals(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        page = await client.get_deals(direction=ItemDealDirections.OUT, count=PAGE_SIZE, after_cursor=cursor(request))
        return web.json_response(paginate(page, "deals", deal_json))

    async def listings(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        scope = request.query.get("status")
        if scope and scope not in {"APPROVED", "DRAFT", "SOLD"}:
            raise web.HTTPBadRequest(reason="Неизвестный статус товара.")
        states = [ItemStatuses[scope]] if scope else None
        page = await client.call("get_my_items", statuses=states, count=PAGE_SIZE, after_cursor=cursor(request))
        return web.json_response(paginate(page, "items", listing_json))

    async def catalog_games(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        query = request.query.get("q", "").strip()
        if len(query) > 120:
            raise web.HTTPBadRequest(reason="Введите название не длиннее 120 символов.")
        client = await self.gateway.get_client(account)
        page = await client.call("get_games", name=query or None, count=PAGE_SIZE, after_cursor=cursor(request))
        return web.json_response(paginate(page, "games", lambda game: {
            **named_entry(game), "cover": media_url(getattr(getattr(game, "logo", None), "url", None)),
        }))

    async def catalog_game(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        game_id = resource_id(request.match_info["game_id"])
        game = await client.call("get_game", id=game_id)
        if not game or str(game.id) != game_id:
            raise web.HTTPNotFound(reason="Раздел каталога не найден.")
        return web.json_response({
            **named_entry(game),
            "categories": [named_entry(entry) for entry in getattr(game, "categories", None) or [] if entry and entry.id],
        })

    async def catalog_category(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        category_id = resource_id(request.match_info["category_id"])
        method = request.query.get("method")
        schema, _ = await catalog_schema(client, category_id, resource_id(method) if method else None)
        return web.json_response(schema)

    async def create_listing(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        if request.content_type == "application/json":
            body, photos = await read_json(request), []
        else:
            body, photos = await read_draft_multipart(request)
        client = await self.gateway.get_client(account)
        payload = await draft_payload(client, body)
        key = str(account.id)
        lock = self.listing_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            raise web.HTTPConflict(reason="Дождитесь завершения предыдущего действия с лотом.")
        async with lock:
            try:
                draft = await client.create_item(**payload, attachments=photos)
            except (RequestFailedError, RequestSendingError, RequestPlayerokError) as exc:
                raise web.HTTPBadGateway(reason=playerok_error_text(exc) + " Перед повтором проверьте черновики.") from exc
            except (KeyError, ValueError, TypeError) as exc:
                raise web.HTTPBadGateway(reason="Playerok не подтвердил создание черновика. Проверьте список перед повтором.") from exc
            if draft is None or not getattr(draft, "id", None):
                raise web.HTTPBadGateway(reason="Playerok не подтвердил создание черновика. Проверьте список перед повтором.")
        return web.json_response(listing_details_json(draft), status=201)

    async def listing_quote(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        item = await self.owned_listing(client, account, request.match_info["item_id"])
        if not can_publish(item):
            raise web.HTTPConflict(reason="Playerok пока не разрешает опубликовать этот лот.")
        prices = (int(item.price), tariff_price(item))
        tariffs = await publication_tariffs(client, str(item.id), prices[1])
        balance = await publication_balance(client)
        now = time.monotonic()
        self.publication_quotes = {key: value for key, value in self.publication_quotes.items() if value["expires"] > now}
        quote_token = uuid.uuid4().hex
        self.publication_quotes[(str(account.id), str(item.id))] = {
            "token": quote_token, "prices": prices, "tariffs": {entry.id: entry for entry in tariffs},
            "balance": balance, "expires": now + PUBLICATION_QUOTE_TTL,
        }
        return web.json_response({
            "token": quote_token, "itemId": str(item.id), "name": item.name, "price": prices[0], "basePrice": prices[1],
            "balance": format(balance, "f") if balance is not None else None,
            "tariffs": [{
                "id": entry.id, "name": entry.name, "fee": format(entry.fee, "f"),
                "available": entry.fee == 0 or balance is not None and balance >= entry.fee,
                "remaining": format(balance - entry.fee, "f") if balance is not None and balance >= entry.fee else None,
            } for entry in tariffs],
        })

    async def publish_listing(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        item_id = resource_id(request.match_info["item_id"])
        body = await read_json(request)
        if body.get("confirm") is not True or not isinstance(body.get("token"), str) or not isinstance(body.get("tariffId"), str):
            raise web.HTTPBadRequest(reason="Выберите тариф и подтвердите публикацию.")
        key = (str(account.id), item_id)
        lock = self.listing_locks.setdefault(key[0], asyncio.Lock())
        if lock.locked():
            raise web.HTTPConflict(reason="Дождитесь завершения предыдущего действия с лотом.")
        async with lock:
            quote = self.publication_quotes.get(key)
            if not quote or quote["expires"] <= time.monotonic() or quote["token"] != body["token"]:
                raise web.HTTPConflict(reason="Условия публикации устарели. Обновите тарифы.")
            tariff = quote["tariffs"].get(body["tariffId"])
            if not tariff or tariff.fee > 0 and (quote["balance"] is None or quote["balance"] < tariff.fee):
                raise web.HTTPBadRequest(reason="Тариф недоступен или недостаточно средств.")
            # One-time confirmation; never retry a mutation automatically.
            self.publication_quotes.pop(key, None)
            client = await self.gateway.get_client(account)
            await self.owned_listing(client, account, item_id)
            try:
                result = await confirm_publication(client, item_id, quote["prices"], tariff, quote["balance"])
            except PublicationChanged as exc:
                raise web.HTTPConflict(reason="Цена, тариф или баланс изменились. Обновите условия публикации.") from exc
            except PublicationUnconfirmed as exc:
                raise web.HTTPConflict(reason="Playerok пока не подтвердил размещение. Проверьте статус лота перед повтором.") from exc
            except (RequestFailedError, RequestSendingError, RequestPlayerokError) as exc:
                raise web.HTTPBadGateway(reason=playerok_error_text(exc) + " Проверьте статус лота перед повтором.") from exc
        return web.json_response(listing_details_json(result))

    @staticmethod
    async def owned_listing(client: Any, account: PlayerokAccount, item_id: str) -> Any:
        item = await client.get_item(resource_id(item_id))
        owner = getattr(getattr(item, "user", None), "id", None)
        if item is None or (str(owner) != account.playerok_user_id and (owner or not isinstance(item, MyItem))):
            raise web.HTTPNotFound(reason="Лот этого аккаунта не найден.")
        return item

    async def listing(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        item = await self.owned_listing(client, account, request.match_info["item_id"])
        if request.method == "GET":
            return web.json_response(listing_details_json(item))
        if getattr(item, "is_editable", None) is False:
            raise web.HTTPForbidden(reason="Playerok пока не разрешает редактировать этот лот.")
        body = await read_json(request)
        if not body or set(body) - {"name", "description", "price", "comment"}:
            raise web.HTTPBadRequest(reason="Некорректные поля лота.")
        patch: dict[str, Any] = {}
        for key, label, limit in (("name", "Название", 120), ("description", "Описание", 4096)):
            if key in body:
                value = body[key]
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
                    raise web.HTTPBadRequest(reason=f"{label} должно содержать от 1 до {limit} символов.")
                patch[key] = value.strip()
        if "price" in body:
            if type(body["price"]) is not int or not 1 <= body["price"] <= 10_000_000:
                raise web.HTTPBadRequest(reason="Введите целую цену от 1 до 10 000 000 ₽.")
            patch["price"] = body["price"]
        if "comment" in body:
            text = body["comment"]
            if not isinstance(text, str) or not valid_buyer_message(text):
                raise web.HTTPBadRequest(reason=BUYER_MESSAGE_ERROR)
            try:
                patch.update(await listing_comment_update(client, item, text.strip()))
            except ListingFieldsUnavailable as exc:
                raise web.HTTPConflict(reason=str(exc)) from exc
        updated = await client.update_item(str(item.id), **patch)
        if updated is None:
            raise web.HTTPBadGateway(reason="Playerok не подтвердил сохранение. Обновите лот перед повтором.")
        return web.json_response(listing_details_json(updated))

    async def photo(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        client = await self.gateway.get_client(account)
        item = await self.owned_listing(client, account, request.match_info["item_id"])
        if getattr(item, "is_editable", None) is False:
            raise web.HTTPForbidden(reason="Playerok пока не разрешает редактировать этот лот.")
        if request.content_type != "multipart/form-data":
            raise web.HTTPUnsupportedMediaType(reason="Ожидается файл изображения.")
        remove = request.query.get("replace")
        attachments = getattr(item, "attachments", None) or []
        if remove and remove not in {str(entry.id) for entry in attachments}:
            raise web.HTTPBadRequest(reason="Фотография лота не найдена.")
        if not remove and len(attachments) >= 10:
            raise web.HTTPBadRequest(reason="У лота уже десять фотографий. Выберите фото для замены.")
        reader = await request.multipart()
        entry = await reader.next()
        if entry is None or entry.name != "image" or not entry.filename:
            raise web.HTTPBadRequest(reason="Выберите фотографию.")
        picture = bytearray()
        while chunk := await entry.read_chunk(65536):
            picture.extend(chunk)
            if len(picture) > MAX_PHOTO_SIZE:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_PHOTO_SIZE, actual_size=len(picture))
        if not valid_photo(picture):
            raise web.HTTPBadRequest(reason="Поддерживаются изображения JPEG, PNG и WebP.")
        updated = await client.update_item(
            str(item.id), add_attachments=[bytes(picture)], remove_attachments=[remove] if remove else None,
        )
        if updated is None:
            raise web.HTTPBadGateway(reason="Playerok не подтвердил загрузку. Обновите лот перед повтором.")
        return web.json_response(listing_details_json(updated))

    async def telegram_plugin(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        if request.method == "GET":
            return web.json_response(await self.telegram_plugins.panel(account))
        return web.json_response(await self.telegram_plugins.configure(account, await read_json(request)))

    async def telegram_plugin_ai(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        return web.json_response(await self.telegram_plugins.configure_ai(account, await read_json(request)))

    async def telegram_plugin_bind(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        body = await read_json(request)
        country = country_value(body.get("country"))
        if "keyword" in body:
            if "lotId" in body:
                raise web.HTTPBadRequest(reason="Выберите лот или укажите слово из его названия.")
            return web.json_response(await self.telegram_plugins.bind_keyword(account, body["keyword"], country), status=201)
        client = await self.gateway.get_client(account)
        item = await self.owned_listing(client, account, resource_id(body.get("lotId")))
        return web.json_response(await self.telegram_plugins.bind(account, item, country), status=201)

    async def telegram_plugin_rule(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        rule_id = int(request.match_info["rule_id"])
        body = None if request.method == "DELETE" else await read_json(request)
        return web.json_response(await self.telegram_plugins.update_rule(account, rule_id, body))

    async def plugin_json(self, account: PlayerokAccount, plugin: PluginSpec) -> dict[str, Any]:
        enabled, config = await self.plugins.resolved_state(account.id, plugin)
        fields = []
        for key, meta in plugin.settings.items():
            private = is_secret(key, meta)
            kind = field_type(meta)
            fields.append({
                "key": key,
                "label": str(meta.get("label", key)),
                "type": kind,
                "secret": private,
                **({"configured": bool(config.get(key))} if private else {"value": config.get(key)}),
                **({"choices": list(meta.get("choices") or [])} if kind == "choice" and not private else {}),
            })
        return {
            "id": plugin.id, "name": plugin.name, "version": plugin.version,
            "description": plugin.description, "enabled": enabled, "fields": fields,
            "telegramAccounts": plugin.id == "telegram_accounts",
        }

    async def installed_plugins(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        return web.json_response({"items": [
            await self.plugin_json(account, plugin) for plugin in self.plugins.ordered()
        ]})

    async def update_plugin(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        plugin = self.plugins.plugins.get(request.match_info["plugin_id"])
        if plugin is None:
            raise web.HTTPNotFound(reason="Плагин не найден.")
        body = await read_json(request)
        enabled = body.get("enabled")
        values = body.get("settings", {})
        if not isinstance(values, dict) or ("enabled" in body and type(enabled) is not bool):
            raise web.HTTPBadRequest(reason="Некорректные настройки.")
        if set(values) - plugin.settings.keys():
            raise web.HTTPBadRequest(reason="В плагине нет такой настройки.")
        normalized = {key: setting_value(plugin.settings[key], value) for key, value in values.items()}
        await self.plugins.configure_state(account.id, plugin, enabled=enabled, values=normalized)
        return web.json_response(await self.plugin_json(account, plugin))

    async def notification_settings(self, request: web.Request) -> web.Response:
        account = await self.account(request)
        values = dict(NOTIFICATIONS)
        current = dict((account.settings or {}).get("notifications") or {})
        values.update({key: bool(current[key]) for key in values if key in current})
        if request.method == "PATCH":
            patch = await read_json(request)
            if not patch or set(patch) - values.keys() or any(type(value) is not bool for value in patch.values()):
                raise web.HTTPBadRequest(reason="Некорректные настройки уведомлений.")
            async with self.db() as session:
                row = await session.get(PlayerokAccount, account.id, with_for_update=True)
                if row is None:
                    raise web.HTTPNotFound(reason="Аккаунт не найден.")
                saved = dict(row.settings or {})
                notifications = {**(saved.get("notifications") or {}), **patch}
                row.settings = {**saved, "notifications": notifications}
                await session.commit()
                values.update({key: bool(notifications[key]) for key in values if key in notifications})
        return web.json_response(values)

    def create_app(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware], client_max_size=64 * 1024)
        app.on_response_prepare.append(self.prepare)
        app.router.add_get("/", self.index)
        app.router.add_get("/healthz", self.health)
        app.router.add_static("/assets/", STATIC_DIR / "assets")
        app.router.add_get("/api/bootstrap", self.bootstrap, name="bootstrap")
        base = "/api/accounts/{account_id}"
        for method, path, handler in (
            ("PATCH", "/active", self.select_account),
            ("GET", "/overview", self.overview),
            ("GET", "/balance", self.balance),
            ("GET", "/withdrawals", self.withdrawals),
            ("POST", "/withdrawals", self.create_payout),
            ("GET", "/withdrawal-methods", self.withdrawal_options),
            ("GET", "/chats", self.chats),
            ("GET", "/chats/{chat_id}", self.history),
            ("POST", "/chats/{chat_id}/reply", self.reply),
            ("POST", "/chats/{chat_id}/read", self.read_chat),
            ("GET", "/deals", self.deals),
            ("GET", "/catalog/games", self.catalog_games),
            ("GET", "/catalog/games/{game_id}", self.catalog_game),
            ("GET", "/catalog/categories/{category_id}", self.catalog_category),
            ("GET", "/listings", self.listings),
            ("POST", "/listings", self.create_listing),
            ("GET", "/listings/{item_id}/publication", self.listing_quote),
            ("POST", "/listings/{item_id}/publication", self.publish_listing),
            ("GET", "/listings/{item_id}", self.listing),
            ("PATCH", "/listings/{item_id}", self.listing),
            ("POST", "/listings/{item_id}/photo", self.photo),
            ("GET", "/telegram-accounts", self.telegram_plugin),
            ("PATCH", "/telegram-accounts", self.telegram_plugin),
            ("PATCH", "/telegram-accounts/ai", self.telegram_plugin_ai),
            ("POST", "/telegram-accounts/rules", self.telegram_plugin_bind),
            ("PATCH", r"/telegram-accounts/rules/{rule_id:\d{1,10}}", self.telegram_plugin_rule),
            ("DELETE", r"/telegram-accounts/rules/{rule_id:\d{1,10}}", self.telegram_plugin_rule),
            ("GET", "/plugins", self.installed_plugins),
            ("PATCH", "/plugins/{plugin_id}", self.update_plugin),
            ("GET", "/notifications", self.notification_settings),
            ("PATCH", "/notifications", self.notification_settings),
        ):
            app.router.add_route(method, base + path, handler, name=f"{handler.__name__}-{method}")
        return app


async def start_miniapp(
    settings: Settings, db: async_sessionmaker[AsyncSession], gateway: PlayerokGateway, plugins: PluginManager,
    *, bot_profile: Any = None,
) -> web.AppRunner:
    runner = web.AppRunner(MiniApp(db, gateway, plugins, settings.bot_token, bot_profile).create_app(), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", settings.port).start()
    except BaseException:
        await runner.cleanup()
        raise
    logger.info("Mini App HTTP server listening on 0.0.0.0:%s", settings.port)
    return runner
