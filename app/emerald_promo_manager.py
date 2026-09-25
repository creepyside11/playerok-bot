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
TELEGRAPH_DOCS_URL = "https://telegra.ph/Emerald-AI--Dokumentaciya-i-Podklyuchenie-09-25"
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


FALLBACK_MODELS = [
    ("claude-fable-5-1", "Claude FABLE 5.1X Model", "Anthropic", "×14", "Работает"),
    ("claude-fable-5", "Claude FABLE 5X Model", "Anthropic", "×11", "Работает"),
    ("claude-opus-5", "Claude Opus 5", "Anthropic", "×5", "Работает"),
    ("claude-opus-4-8", "Claude Opus 4.8", "Anthropic", "×5", "Работает"),
    ("claude-sonnet-5", "Claude Sonnet 5", "Anthropic", "×3.5", "Работает"),
    ("claude-opus-4-6", "Claude Opus 4.6", "Anthropic", "×2", "Работает"),
    ("gpt-6-astra", "GPT 6 AstraX Model", "OpenAI", "×12", "Работает"),
    ("gpt-5-6-sol", "GPT 5.6 SOL", "OpenAI", "×7", "Работает"),
    ("gpt-5-5", "GPT 5.5", "OpenAI", "×1", "Работает"),
    ("gpt-5-4-mini", "GPT 5.4 mini", "OpenAI", "×2.5", "Техработы"),
    ("gpt-image-2", "GPT Image 2", "OpenAI", "", "Техработы"),
    ("gpt-5-6-terra", "GPT 5.6 Terra", "OpenAI", "×1", "Отключена"),
    ("gpt-5-6-luna", "GPT 5.6 Luna", "OpenAI", "×1", "Отключена"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro", "DeepSeek", "×1.5", "Техработы"),
    ("deepseek-v4-flash", "DeepSeek V4 Flash", "DeepSeek", "×0.8", "Работает"),
    ("deepseek-v4-1-flash", "DeepSeek V4.1 Flash", "DeepSeek", "×1", "Отключена"),
    ("gemini-3-1-pro", "Gemini 3.1 Pro", "Google", "×3", "Работает"),
    ("gemini-3-7-flash", "Gemini 3.7 Flash", "Google", "×4", "Работает"),
    ("gemini-3-6-flash", "Gemini 3.6 Flash", "Google", "×3", "Работает"),
    ("grok-4-6", "GROK 4.6X Model", "xAI", "×4", "Работает"),
    ("grok-4-5", "GROK 4.5X Model", "xAI", "×3", "Работает"),
    ("qwen-3-8-max", "Qwen 3.8 MAX", "Alibaba", "×6", "Работает"),
    ("qwen-3-6", "Qwen 3.6", "Alibaba", "×3", "Техработы"),
    ("step-3-7-flash", "Step 3.7 Flash", "Step", "×0.6", "Работает"),
    ("kimi-k3", "Kimi K3", "Moonshot", "×5", "Техработы"),
    ("minimax-m3", "MiniMax M3", "MiniMax", "×1.5", "Техработы"),
    ("nemotron-free", "Nemotron (FREE)", "NVIDIA", "×0", "Работает"),
    ("glm-5-3-flash", "GLM 5.3 Flash", "Zhipu", "×1", "Отключена"),
    ("ox-alpha", "OX Alpha", "OX", "×0.2", "Отключена"),
]

_CACHED_MODELS_TEXT: str | None = None
_CACHED_MODELS_TIME: float = 0.0


def fetch_models_text(telegraph_url: str = TELEGRAPH_DOCS_URL) -> str:
    """Возвращает актуальный список моделей с их статусом, множителем и документацией."""
    global _CACHED_MODELS_TEXT, _CACHED_MODELS_TIME
    import time
    now = time.time()

    if _CACHED_MODELS_TEXT and (now - _CACHED_MODELS_TIME) < 180:
        return _CACHED_MODELS_TEXT

    models: list[tuple[str, str, str, str, str]] = []
    try:
        req = requests.get(
            "https://emeraldai.beer/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=8,
        )
        if req.ok:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(req.text, "html.parser")
                for art in soup.find_all("article"):
                    h4 = art.find("h4")
                    if not h4:
                        continue
                    title = h4.get_text(strip=True)
                    code_el = art.find("code")
                    model_id = code_el.get_text(strip=True) if code_el else ""
                    if not model_id:
                        continue
                    status_el = art.find("span", class_=lambda c: c and "public-model-state" in c)
                    classes = status_el.get("class", []) if status_el else []
                    status_raw = status_el.get_text(strip=True) if status_el else "Работает"
                    meta = art.find("div", class_="product-model-meta")
                    mult = ""
                    if meta:
                        strong = meta.find("strong")
                        if strong:
                            mult = strong.get_text(strip=True)
                    provider_p = art.find("p")
                    provider = provider_p.get_text(strip=True) if provider_p else ""

                    if "offline" in classes or "откл" in status_raw.casefold():
                        status = "Отключена"
                    elif "maintenance" in classes or "тех" in status_raw.casefold():
                        status = "Техработы"
                    else:
                        status = "Работает"

                    models.append((model_id, title, provider, mult, status))
            except Exception:
                pass
    except Exception:
        pass

    if not models:
        models = FALLBACK_MODELS

    lines = [
        "╔══════════════════════════╗",
        "║  📋 СПИСОК МОДЕЛЕЙ И СТАТУС",
        "╚══════════════════════════╝",
        "🟢 Работает | 🟡 Техработы | 🔴 Отключена\n",
    ]

    for model_id, title, provider, mult, status in models:
        s_lower = status.casefold()
        if "работ" in s_lower:
            icon = "🟢"
        elif "тех" in s_lower:
            icon = "🟡"
        else:
            icon = "🔴"
        mult_str = f" · {mult}" if mult else ""
        lines.append(f"{icon} {model_id}{mult_str} ({status})")

    lines.append("\n" + "─" * 26)
    lines.append(f"📖 Документация в Telegraph:\n{telegraph_url}\n")
    lines.append("💡 Для проверки баланса ключа напишите: #баланс")

    res_text = "\n".join(lines)
    _CACHED_MODELS_TEXT = res_text
    _CACHED_MODELS_TIME = now
    return res_text


def free_message(code: str, token_amount: int, *, repeated: bool = False, is_key: bool = False) -> str:
    heading = "ВАШ БЕСПЛАТНЫЙ ДОСТУП" if not repeated else "ВАШ ТЕСТОВЫЙ ДОСТУП"
    item_label = "🔑 API-ключ" if is_key else "🔑 Промокод"
    item_hint = (
        f"📖 Документация в Telegraph:\n{TELEGRAPH_DOCS_URL}\n\n"
        "⚙️ Base URL для клиентов: https://emeraldai.beer/v1\n\n"
        "Ключ уже готов к использованию без регистрации!\n"
        "💡 Для проверки остатка напишите: #баланс\n"
        "📋 Список моделей и статус: #модели"
        if is_key else
        f"📖 Инструкция в Telegraph:\n{TELEGRAPH_DOCS_URL}\n\n"
        "Промокод одноразовый и предназначен для тестирования моделей EmeraldAI.\n"
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
        f"📖 Документация в Telegraph:\n{TELEGRAPH_DOCS_URL}\n\n"
        "⚙️ Base URL: https://emeraldai.beer/v1\n\n"
        "Ключ активен сразу! Вы можете использовать его в Cursor, Claude Code, Python SDK и любых совместимых клиентах.\n\n"
        "💬 Команды в этом чате:\n"
        "• #баланс — проверить остаток токенов\n"
        "• #модели — актуальный список моделей и их статус"
        if is_key else
        f"📖 Инструкция по активации в Telegraph:\n{TELEGRAPH_DOCS_URL}\n\n"
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
        f"📖 Документация в Telegraph:\n{TELEGRAPH_DOCS_URL}\n"
        "⚙️ Base URL: https://emeraldai.beer/v1\n\n"
        "💡 Для быстрой проверки баланса напишите: #баланс\n"
        "📋 Список моделей и статус: #модели"
        if is_key else
        f"📖 Инструкция в Telegraph:\n{TELEGRAPH_DOCS_URL}\n\n"
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
