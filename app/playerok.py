from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import threading
from dataclasses import dataclass
from typing import Any

import certifi
from curl_cffi import requests as curl_requests
import playerokapi.account as playerok_account_module
from playerokapi.account import Account
from playerokapi.enums import ItemDealStatuses

from .crypto import SecretCipher
from .models import PlayerokAccount


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)

GET_EMAIL_AUTH_CODE = """mutation getEmailAuthCode($email: String!) {
  getEmailAuthCode(input: {email: $email})
}"""

CHECK_EMAIL_AUTH_CODE = """mutation checkEmailAuthCode($input: CheckEmailAuthCodeInput!) {
  checkEmailAuthCode(input: $input) {
    id username email role hasFrozenBalance isBlocked isBlockedFor canPublishItems
    balance { value __typename }
    profile { id avatarURL testimonialCounter __typename }
    __typename
  }
}"""

VIEWER_QUERY = """query viewer {
  viewer {
    id username email role hasFrozenBalance isBlocked isBlockedFor canPublishItems createdAt
    balance { value __typename }
    profile { id avatarURL testimonialCounter __typename }
    __typename
  }
}"""


_PLAYEROK_INIT_LOCK = threading.RLock()


class _PlayerokShutilProxy:
    """Fallback for a packaging bug in PlayerokAPI.

    Upstream copies playerokapi/cacert.pem during Account.__init__, but its
    setup.py does not package that file. On pip/git installs the source path can
    therefore be missing. Use certifi's CA bundle only for that missing file.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(shutil, name)

    @staticmethod
    def copyfile(src: str, dst: str, *args: Any, **kwargs: Any) -> str:
        source = src
        if not os.path.exists(source) and os.path.basename(source) == "cacert.pem":
            source = certifi.where()
        return shutil.copyfile(source, dst, *args, **kwargs)


class MultiAccount(Account):
    # Upstream Account is a singleton. The bot needs isolated sessions.
    def __new__(cls, *args: Any, **kwargs: Any) -> "MultiAccount":
        return object.__new__(cls)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Account.__init__ resolves shutil from playerokapi.account globals.
        # Swap only that module reference while initialization runs; this avoids
        # patching Python's global shutil module and remains safe across threads.
        with _PLAYEROK_INIT_LOCK:
            original_shutil = playerok_account_module.shutil
            playerok_account_module.shutil = _PlayerokShutilProxy()
            try:
                super().__init__(*args, **kwargs)
            finally:
                playerok_account_module.shutil = original_shutil


def normalize_proxy(raw: str | None) -> str | None:
    if raw is None:
        return None
    proxy = raw.strip()
    if not proxy or proxy.lower() in {"нет", "none", "no", "skip", "-"}:
        return None
    proxy = proxy.replace("https://", "").replace("http://", "")
    parts = proxy.split(":")
    if len(parts) == 4 and "@" not in proxy:
        host, port, user, password = parts
        return f"{user}:{password}@{host}:{port}"
    return proxy


def cookies_to_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)


def parse_cookie_header(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for chunk in value.split(";"):
        if "=" not in chunk:
            continue
        key, val = chunk.split("=", 1)
        if key.strip():
            result[key.strip()] = val.strip()
    return result


@dataclass(slots=True)
class ViewerSnapshot:
    id: str
    username: str
    email: str | None
    balance: str | int | float | None
    is_blocked: bool | None
    can_publish_items: bool | None
    testimonials: int | None


class PlayerokSession:
    def __init__(self, account: MultiAccount):
        self.account = account
        self.lock = asyncio.Lock()

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        async with self.lock:
            return await asyncio.to_thread(getattr(self.account, method), *args, **kwargs)

    async def refresh(self) -> MultiAccount:
        return await self.call("get")

    async def viewer(self) -> ViewerSnapshot:
        async with self.lock:
            def _load() -> ViewerSnapshot:
                self.account.get()
                response = self.account.request(
                    "post",
                    f"{self.account.base_url}/graphql",
                    {"accept": "*/*"},
                    {"operationName": "viewer", "query": VIEWER_QUERY, "variables": {}},
                ).json()
                data = (response.get("data") or {}).get("viewer") or {}
                profile = data.get("profile") or {}
                return ViewerSnapshot(
                    id=str(data.get("id") or self.account.id or ""),
                    username=str(data.get("username") or self.account.username or ""),
                    email=data.get("email") or self.account.email,
                    balance=(data.get("balance") or {}).get("value"),
                    is_blocked=data.get("isBlocked", self.account.is_blocked),
                    can_publish_items=data.get("canPublishItems", self.account.can_publish_items),
                    testimonials=profile.get("testimonialCounter"),
                )
            return await asyncio.to_thread(_load)

    async def get_deals(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call("get_deals", *args, **kwargs)

    async def get_chats(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call("get_chats", *args, **kwargs)

    async def get_my_reviews(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call("get_my_reviews", *args, **kwargs)

    async def send_message(
        self,
        chat_id: str,
        text: str | None = None,
        images: list[str | bytes] | None = None,
        mark_chat_as_read: bool = True,
    ) -> Any:
        return await self.call(
            "send_message",
            chat_id=chat_id,
            text=text,
            images=images or [],
            mark_chat_as_read=mark_chat_as_read,
        )

    async def update_deal(self, deal_id: str, status: ItemDealStatuses) -> Any:
        return await self.call("update_deal", deal_id=deal_id, new_status=status)

    async def get_item(self, item_id: str, slug: str | None = None) -> Any:
        return await self.call("get_item", id=item_id, slug=slug)

    async def update_item(
        self,
        item_id: str,
        name: str | None = None,
        price: int | None = None,
        description: str | None = None,
        options: Any = None,
        data_fields: Any = None,
        remove_attachments: list[str] | None = None,
        add_attachments: list[str | bytes] | None = None,
    ) -> Any:
        return await self.call(
            "update_item",
            id=item_id,
            name=name,
            price=price,
            description=description,
            options=options,
            data_fields=data_fields,
            remove_attachments=remove_attachments,
            add_attachments=add_attachments,
        )

    async def remove_item(self, item_id: str) -> Any:
        return await self.call("remove_item", id=item_id)

    async def get_item_priority_statuses(self, item_id: str, item_price: int | str) -> Any:
        return await self.call("get_item_priority_statuses", item_id=item_id, item_price=item_price)

    async def publish_item(self, item_id: str, priority_status_id: str) -> Any:
        return await self.call("publish_item", item_id=item_id, priority_status_id=priority_status_id)


class PlayerokGateway:
    def __init__(self, cipher: SecretCipher):
        self.cipher = cipher
        self._cache: dict[str, tuple[str, PlayerokSession]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _fingerprint(record: PlayerokAccount) -> str:
        raw = "|".join((record.cookies_encrypted, record.proxy_encrypted or "", record.user_agent))
        return hashlib.sha256(raw.encode()).hexdigest()

    async def get_client(self, record: PlayerokAccount) -> PlayerokSession:
        key = str(record.id)
        fingerprint = self._fingerprint(record)
        async with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == fingerprint:
                return cached[1]
            account = MultiAccount(
                cookies=self.cipher.decrypt(record.cookies_encrypted) or "",
                proxy=self.cipher.decrypt(record.proxy_encrypted),
                user_agent=record.user_agent or DEFAULT_USER_AGENT,
                requests_timeout=25,
            )
            client = PlayerokSession(account)
            self._cache[key] = (fingerprint, client)
        await client.refresh()
        return client

    async def invalidate(self, account_id: Any) -> None:
        async with self._lock:
            self._cache.pop(str(account_id), None)

    async def validate_raw(
        self, cookies: str, proxy: str | None, user_agent: str = DEFAULT_USER_AGENT
    ) -> ViewerSnapshot:
        account = MultiAccount(
            cookies=cookies,
            proxy=normalize_proxy(proxy),
            user_agent=user_agent,
            requests_timeout=25,
        )
        return await PlayerokSession(account).viewer()


class EmailAuthClient:
    def __init__(self, user_agent: str = DEFAULT_USER_AGENT):
        self.user_agent = user_agent

    @staticmethod
    def _proxy_url(proxy: str | None) -> str | None:
        value = normalize_proxy(proxy)
        return f"http://{value}" if value else None

    def _session(self, proxy: str | None, cookies: dict[str, str] | None = None):
        session = curl_requests.Session(
            impersonate="chrome",
            timeout=30,
            proxy=self._proxy_url(proxy),
        )
        session.headers.update({
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://playerok.com",
            "referer": "https://playerok.com/profile/auth",
            "user-agent": self.user_agent,
            "apollo-require-preflight": "true",
            "apollographql-client-name": "web",
            "x-gql-path": "/",
            "x-timezone-offset": "-180",
        })
        if cookies:
            session.cookies.update(cookies)
        return session

    @staticmethod
    def _error(data: dict[str, Any]) -> str:
        errors = data.get("errors") or []
        if errors:
            return str(errors[0].get("message") if isinstance(errors[0], dict) else errors[0])
        return "Playerok returned an unexpected response"

    async def request_code(self, email: str, proxy: str | None) -> dict[str, str]:
        email = email.strip()
        if not email or "@" not in email:
            raise ValueError("Некорректный email")

        def _request() -> dict[str, str]:
            session = self._session(proxy)
            response = session.post(
                "https://playerok.com/graphql",
                headers={
                    "x-gql-op": "getEmailAuthCode",
                    "x-apollo-operation-name": "getEmailAuthCode",
                },
                json={
                    "operationName": "getEmailAuthCode",
                    "query": GET_EMAIL_AUTH_CODE,
                    "variables": {"email": email},
                },
            )
            data = response.json()
            if response.status_code >= 400 or not (data.get("data") or {}).get("getEmailAuthCode"):
                raise RuntimeError(self._error(data))
            return {str(k): str(v) for k, v in session.cookies.get_dict().items()}

        return await asyncio.to_thread(_request)

    async def verify_code(
        self,
        email: str,
        code: str,
        proxy: str | None,
        session_cookies: dict[str, str],
    ) -> tuple[str, dict[str, Any]]:
        code = re.sub(r"\D+", "", code)
        if not code:
            raise ValueError("Код пуст")

        def _request() -> tuple[str, dict[str, Any]]:
            session = self._session(proxy, session_cookies)
            response = session.post(
                "https://playerok.com/graphql",
                headers={
                    "x-gql-op": "checkEmailAuthCode",
                    "x-apollo-operation-name": "checkEmailAuthCode",
                },
                json={
                    "operationName": "checkEmailAuthCode",
                    "query": CHECK_EMAIL_AUTH_CODE,
                    "variables": {"input": {"email": email.strip(), "code": code}},
                },
            )
            data = response.json()
            viewer = (data.get("data") or {}).get("checkEmailAuthCode")
            if response.status_code >= 400 or not isinstance(viewer, dict):
                raise RuntimeError(self._error(data))
            cookies = {str(k): str(v) for k, v in session.cookies.get_dict().items()}
            if not cookies.get("token"):
                raise RuntimeError("Playerok подтвердил код, но не вернул token cookie")
            return cookies_to_header(cookies), viewer

        return await asyncio.to_thread(_request)
