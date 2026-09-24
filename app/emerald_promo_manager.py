from __future__ import annotations

import asyncio
import hashlib
import html
import logging
from typing import Any
from urllib.parse import urlsplit

import requests
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, PlayerokAccount


logger = logging.getLogger("emerald_promo")

DEFAULT_API_URL = "https://www.emeraldai.sbs/seller/v1"
ACTIVATION_URL = "https://emeraldai.beer"
BEER_ACCOUNT_URL = "https://www.emeraldai.beer/v1/account"
MIN_TOKEN_AMOUNT = 10_000
MAX_TOKEN_AMOUNT = 1_000_000_000
DEFAULT_FREE_TOKENS = 200_000
DEFAULT_REVIEW_TOKENS = 1_000_000
DEFAULT_MIN_AGE_DAYS = 7


class EmeraldPromoSetting(Base):
    __tablename__ = "emerald_promo_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    api_base_url: Mapped[str] = mapped_column(String(512), default=DEFAULT_API_URL, nullable=False)
    api_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    free_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    free_token_amount: Mapped[int] = mapped_column(BigInteger, default=DEFAULT_FREE_TOKENS, nullable=False)
    min_account_age_days: Mapped[int] = mapped_column(Integer, default=DEFAULT_MIN_AGE_DAYS, nullable=False)
    issue_type: Mapped[str] = mapped_column(String(32), default="api_key", nullable=False)
    key_target: Mapped[str] = mapped_column(String(64), default="funpay_shared", nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EmeraldPromoLotRule(Base):
    __tablename__ = "emerald_promo_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    tokens_per_unit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    review_bonus_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_bonus_tokens: Mapped[int] = mapped_column(BigInteger, default=DEFAULT_REVIEW_TOKENS, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_emerald_lot_rule"),
    )


class EmeraldPromoIssue(Base):
    __tablename__ = "emerald_promo_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # sale, free, review
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    buyer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lot_title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    purchased_units: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    token_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    review_bonus_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_bonus_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    promo_code: Mapped[str | None] = mapped_column(String(256), nullable=True)
    api_promo_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="creating", nullable=False)  # creating, created, sent, failed
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "kind", "source_id", name="uq_emerald_issue"),
    )


def format_tokens(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def promo_prefix(account_id: Any, kind: str, source_id: str) -> str:
    marker = {"free": "F", "sale": "S", "review": "R"}.get(kind, "E")
    digest = hashlib.sha256(f"{account_id}:{kind}:{source_id}".encode("utf-8")).hexdigest().upper()
    return marker + digest[:9]


def validate_api_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Base URL должен быть полным HTTPS-адресом.")
    return normalized


def validate_token_amount(value: str | int) -> int:
    try:
        val = int(str(value).strip().replace(" ", ""))
    except ValueError:
        raise ValueError("Номинал должен быть целым числом.")
    if not MIN_TOKEN_AMOUNT <= val <= MAX_TOKEN_AMOUNT:
        raise ValueError(f"Номинал должен быть от {format_tokens(MIN_TOKEN_AMOUNT)} до {format_tokens(MAX_TOKEN_AMOUNT)} токенов.")
    return val


class EmeraldClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()

    def _request(self, method: str, path: str = "", *, json_payload: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("API-токен продавца не настроен")
        url = f"{self.base_url}/{path.lstrip('/')}".rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        res = requests.request(method, url, headers=headers, json=json_payload, params=params, timeout=25, allow_redirects=True)
        try:
            payload = res.json()
        except Exception as exc:
            raise RuntimeError(f"Emerald API вернул HTTP {res.status_code} без JSON") from exc
        if not res.ok:
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = str(err.get("code") or f"http_{res.status_code}")
            msg = str(err.get("message") or "неизвестная ошибка")
            raise RuntimeError(f"{code}: {msg}")
        return payload

    def get_account(self) -> dict[str, Any]:
        res = self._request("GET", "account")
        data = res.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Emerald API не вернул данные аккаунта")
        return data

    def create_api_key(self, amount: int, name: str, target: str = "funpay_shared") -> dict[str, Any]:
        acc = self.get_account()
        bal = int(acc.get("balance_tokens") or 0)
        if bal < amount:
            raise RuntimeError(f"Недостаточно баланса Emerald: доступно {format_tokens(bal)}, требуется {format_tokens(amount)}")
        res = self._request("POST", "api-keys", json_payload={
            "name": name[:64],
            "token_amount": amount,
            "quantity": 1,
            "target": target,
        })
        data = res.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise RuntimeError("Emerald API не вернул созданный API-ключ")
        return data[0]

    def create_promo_code(self, amount: int, prefix: str) -> dict[str, Any]:
        acc = self.get_account()
        bal = int(acc.get("balance_tokens") or 0)
        if bal < amount:
            raise RuntimeError(f"Недостаточно баланса Emerald: доступно {format_tokens(bal)}, требуется {format_tokens(amount)}")
        res = self._request("POST", "promo-codes", json_payload={
            "type": "tokens",
            "token_amount": amount,
            "quantity": 1,
            "prefix": prefix[:10].upper(),
        })
        data = res.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise RuntimeError("Emerald API не вернул созданный промокод")
        return data[0]

    def recover_promo_code(self, amount: int, prefix: str) -> dict[str, Any] | None:
        for offset in range(0, 500, 100):
            res = self._request("GET", "promo-codes", params={"limit": 100, "offset": offset})
            data = res.get("data")
            if not isinstance(data, list):
                return None
            for promo in data:
                if not isinstance(promo, dict):
                    continue
                code = str(promo.get("code") or "").upper()
                if (
                    code.startswith(prefix.upper() + "-")
                    and str(promo.get("type") or "").casefold() == "tokens"
                    and int(promo.get("token_amount") or 0) == amount
                ):
                    return promo
            if len(data) < 100:
                break
        return None


def free_message(code: str, token_amount: int, *, repeated: bool = False, is_key: bool = False) -> str:
    heading = "ВАШ БЕСПЛАТНЫЙ ДОСТУП" if not repeated else "ВАШ ТЕСТОВЫЙ ДОСТУП"
    item_label = "🔑 API-ключ" if is_key else "🔑 Промокод"
    item_hint = (
        f"🌐 Инструкция и проверка баланса: {ACTIVATION_URL}\n"
        f"⚙️ Base URL для клиентов/SDK: {ACTIVATION_URL}/v1\n\n"
        "Ключ уже готов к использованию без регистрации на сайте! "
        "Для проверки остатка напишите в этот чат: #баланс"
        if is_key else
        f"🌐 Активировать: {ACTIVATION_URL}\n\n"
        "Промокод одноразовый и предназначен для тестирования моделей EmeraldAI. "
        "Бесплатный доступ выдаётся покупателю только один раз."
    )
    return (
        "╔══════════════════════════╗\n"
        f"║  🎁 {heading}\n"
        "╚══════════════════════════╝\n\n"
        f"{item_label}: {code}\n"
        f"🪙 Номинал: {format_tokens(token_amount)} токенов\n\n"
        f"{item_hint}"
    )


def sale_message(code: str, token_amount: int, purchased_units: int, review_bonus_enabled: bool, review_bonus_tokens: int | None, *, is_key: bool = False) -> str:
    title = "💎 КЛЮЧ EMERALDAI" if is_key else "💎 ПРОМОКОД EMERALDAI"
    item_label = "🔑 Ваш API-ключ" if is_key else "🔑 Промокод"
    instructions = (
        f"🌐 Портал и проверка баланса: {ACTIVATION_URL}\n"
        f"⚙️ Base URL: {ACTIVATION_URL}/v1\n\n"
        "Ключ активен сразу! Вы можете использовать его в любых OpenAI/Anthropic клиентах.\n"
        "💡 Для проверки баланса ключа прямо в этом чате отправьте команду:\n"
        "#баланс"
        if is_key else
        f"🌐 Активировать: {ACTIVATION_URL}\n\n"
        "Код можно активировать один раз. Никому не передавайте его до активации."
    )
    text = (
        "╔══════════════════════════╗\n"
        f"║  {title}\n"
        "╚══════════════════════════╝\n\n"
        f"{item_label}: {code}\n"
        f"🪙 Баланс токенов: {format_tokens(token_amount)}\n"
        f"📦 Куплено единиц: {purchased_units}\n\n"
        f"{instructions}"
    )
    if review_bonus_enabled and int(review_bonus_tokens or 0) > 0:
        bonus_type = "API-ключ" if is_key else "промокод"
        text += (
            "\n\n┏━━━━━━━━ 🎁 БОНУС ЗА ОТЗЫВ ━━━━━━━━┓\n"
            "Оставьте этому заказу отзыв ровно на 5 звёзд — бот автоматически "
            f"выдаст ещё один {bonus_type} на {format_tokens(review_bonus_tokens or 0)} токенов.\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
        )
    return text


def review_message(code: str, token_amount: int, *, is_key: bool = False) -> str:
    item_label = "🔑 Ваш бонусный API-ключ" if is_key else "🔑 Бонусный промокод"
    hint = (
        f"🌐 Проверка баланса: {ACTIVATION_URL}\n"
        f"⚙️ Base URL: {ACTIVATION_URL}/v1\n"
        "Для быстрой проверки баланса напишите: #баланс"
        if is_key else
        f"🌐 Активировать: {ACTIVATION_URL}\n"
        "Промокод одноразовый."
    )
    return (
        "╔══════════════════════════╗\n"
        "║  ⭐ БОНУС ЗА ОТЗЫВ 5★\n"
        "╚══════════════════════════╝\n\n"
        "Спасибо за отличную оценку!\n"
        f"{item_label}: {code}\n"
        f"🪙 Номинал: {format_tokens(token_amount)} токенов\n\n"
        f"{hint}"
    )
