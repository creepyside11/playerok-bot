"""Creating Playerok payouts with validated requisites.

Playerok does not document its GraphQL API. The contract below mirrors the
official site (`mutation requestWithdrawal`) as of the current build:

    requestWithdrawal(input: {
      provider: TransactionProviderId!,
      value: number!,                      # целые рубли
      account: string!,                    # карта/телефон/кошелёк/ID карты
      providerData: { paymentMethodId, sbpBankMemberId }
    })

A payout is money leaving the seller's account, so every field is re-validated
against fresh server data right before the mutation is sent.
"""

from __future__ import annotations

import re
import uuid
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from playerokapi.enums import (
    TransactionPaymentMethodIds, TransactionProviderDirections, TransactionProviderIds,
)

from .item_publication import amount
from .playerok_errors import playerok_error_text
from .withdrawal_history import payout_json

PLAYEROK_WALLET_URL = "https://playerok.com/wallet/withdraw"
CARD_BINDING_HINT = (
    "Карту привязывает сам Playerok: откройте «Кошелёк → Вывод», добавьте карту и "
    "пройдите проверку банка. После этого она появится здесь."
)

class WithdrawalError(ValueError):
    """Refusal that is safe to show to the seller as-is."""

# provider -> how its `account` field must be filled
REQUISITE_KINDS = {
    "BANK_CARD_RU": "card_id",
    "BANK_CARD": "card_number",
    "BANK_CARD_BY": "account",
    "SBP": "phone",
    "YMONEY": "phone",
    "USDT": "wallet",
}
REQUISITE_TITLES = {
    "card_id": "карта из списка привязанных",
    "card_number": "номер карты",
    "phone": "номер телефона",
    "wallet": "адрес кошелька USDT (TRC20)",
    "account": "номер счёта",
}
REQUISITE_PROMPTS = {
    "card_id": "Выберите привязанную карту кнопкой ниже.",
    "card_number": "Введите номер карты (13–19 цифр).",
    "phone": "Введите номер телефона в формате +7 999 123-45-67.",
    "wallet": "Введите адрес кошелька USDT TRC20 (начинается с T, 34 символа).",
    "account": "Введите номер счёта для выплаты.",
}
MAX_PAYOUT_RUBLES = 10_000_000
TRC20 = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")
UUID_VALUE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

def enum_name(value: Any) -> str:
    return str(getattr(value, "name", value) or "")

def requisite_kind(provider_id: Any) -> str:
    return REQUISITE_KINDS.get(enum_name(provider_id), "account")

def parse_amount(raw: Any) -> int:
    """Whole rubles only: the site input has no fractional part either."""
    text = str(raw or "").strip().replace(" ", "").replace("\u00a0", "")
    if not re.fullmatch(r"[0-9]{1,10}", text):
        raise WithdrawalError("Введите сумму целыми рублями, например 500.")
    value = int(text)
    if value <= 0:
        raise WithdrawalError("Сумма должна быть больше нуля.")
    if value > MAX_PAYOUT_RUBLES:
        raise WithdrawalError("Слишком большая сумма для одной выплаты.")
    return value

def luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0

def parse_card_number(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if not 13 <= len(digits) <= 19:
        raise WithdrawalError("Номер карты должен содержать 13–19 цифр.")
    if not luhn(digits):
        raise WithdrawalError("Номер карты не проходит проверку контрольной суммы.")
    return digits

def parse_phone(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10 or digits[0] != "9":
        raise WithdrawalError("Введите российский номер телефона, например +7 999 123-45-67.")
    return "+7" + digits

def parse_wallet(raw: Any) -> str:
    value = str(raw or "").strip()
    if not TRC20.fullmatch(value):
        raise WithdrawalError("Неверный адрес кошелька USDT TRC20.")
    return value

def parse_card_id(raw: Any) -> str:
    value = str(raw or "").strip()
    if not UUID_VALUE.fullmatch(value):
        raise WithdrawalError("Выберите карту из списка привязанных.")
    return str(uuid.UUID(value))

def parse_requisites(kind: str, raw: Any) -> str:
    if kind == "card_id":
        return parse_card_id(raw)
    if kind == "card_number":
        return parse_card_number(raw)
    if kind == "phone":
        return parse_phone(raw)
    if kind == "wallet":
        return parse_wallet(raw)
    value = str(raw or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .\-]{5,33}", value):
        raise WithdrawalError("Введите корректный номер счёта (6–34 символа).")
    return value

def mask_requisites(kind: str, value: str) -> str:
    """Show only a short tail; the full value is never echoed back."""
    if kind == "phone":
        tail = value[-2:]
        return f"+7 ••• ••• -{tail[:1]}-{tail[1:]}"
    if kind == "wallet":
        return f"•••• {value[-4:]}"
    return f"•••• {value.replace('-', '')[-4:]}"

def money(value: Any) -> str:
    parsed = amount(value)
    if parsed is None:
        return "—"
    return format(parsed.quantize(Decimal("0.01")), "f").replace(".", ",") + " ₽"

def fee_preview(provider: Any, value: int) -> dict[str, Decimal]:
    """Same arithmetic the site uses: percent, floor by minFeeAmount, RUB only."""
    base = Decimal(value)
    percent = amount(getattr(provider, "fee", None)) or Decimal(0)
    fee = base * percent / 100
    minimum = amount(getattr(provider, "min_fee_amount", None))
    if minimum is not None and fee < minimum:
        fee = minimum
    payout = (base - fee).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    return {"fee": fee.quantize(Decimal("0.01")), "payout": payout}

def decimal_text(value: Any) -> str | None:
    """JSON-safe number: Decimals are not serializable by aiohttp."""
    parsed = amount(value)
    return format(parsed, "f") if parsed is not None else None

def provider_json(provider: Any) -> dict[str, Any] | None:
    identifier = enum_name(getattr(provider, "id", None))
    if identifier not in TransactionProviderIds.__members__:
        return None
    kind = requisite_kind(identifier)
    limits = getattr(getattr(provider, "limits", None), "outgoing", None)
    methods = [
        {
            "id": enum_name(method.id),
            "name": str(getattr(method, "name", "") or ""),
            "fee": decimal_text(getattr(method, "fee", None)),
        }
        for method in (getattr(provider, "payment_methods", None) or [])
        if enum_name(getattr(method, "id", None)) in TransactionPaymentMethodIds.__members__
    ]
    return {
        "id": identifier,
        "name": str(getattr(provider, "name", "") or identifier),
        "requisites": kind,
        "requisitesTitle": REQUISITE_TITLES[kind],
        "requisitesPrompt": REQUISITE_PROMPTS[kind],
        "fee": decimal_text(getattr(provider, "fee", None)),
        "minFee": decimal_text(getattr(provider, "min_fee_amount", None)),
        "minValue": decimal_text(getattr(limits, "min", None)),
        "maxValue": decimal_text(getattr(limits, "max", None)),
        "paymentMethods": methods,
        "needsBank": identifier == "SBP",
        "description": str(getattr(provider, "description", "") or "")[:400],
    }

def card_json(card: Any) -> dict[str, Any] | None:
    identifier = str(getattr(card, "id", "") or "")
    if not UUID_VALUE.fullmatch(identifier):
        return None
    return {
        "id": identifier,
        "firstSix": str(getattr(card, "card_first_six", "") or ""),
        "lastFour": str(getattr(card, "card_last_four", "") or ""),
        "type": enum_name(getattr(card, "card_type", None)),
        "chosen": bool(getattr(card, "is_chosen", False)),
    }

def bank_json(bank: Any) -> dict[str, str] | None:
    identifier = str(getattr(bank, "id", "") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        return None
    return {"id": identifier, "name": str(getattr(bank, "name", "") or "")[:120]}

async def verified_cards(client: Any) -> list[dict[str, Any]]:
    page = await client.call("get_verified_cards")
    cards = (getattr(page, "bank_cards", None) or []) if page is not None else []
    return [entry for entry in (card_json(card) for card in cards) if entry]

async def sbp_banks(client: Any) -> list[dict[str, str]]:
    banks = await client.call("get_sbp_bank_members") or []
    return [entry for entry in (bank_json(bank) for bank in banks) if entry]

async def withdrawal_methods(client: Any) -> dict[str, Any]:
    """Everything the UI needs to fill requisites; read-only."""
    providers = await client.call("get_transaction_providers", TransactionProviderDirections.OUT)
    catalogue = [entry for entry in (provider_json(provider) for provider in providers or []) if entry]
    cards: list[dict[str, Any]] = []
    banks: list[dict[str, str]] = []
    for provider in catalogue:
        if provider["requisites"] == "card_id" and not cards:
            cards = await verified_cards(client)
        if provider["needsBank"] and not banks:
            banks = await sbp_banks(client)
    return {
        "providers": catalogue,
        "cards": cards,
        "banks": banks,
        "walletUrl": PLAYEROK_WALLET_URL,
        "cardHint": CARD_BINDING_HINT,
    }

async def withdrawable(client: Any, own_id: str) -> Decimal:
    viewer = await client.viewer()
    if not own_id or str(getattr(viewer, "id", "")) != str(own_id):
        raise WithdrawalError("Не удалось подтвердить подключённый аккаунт.")
    if getattr(viewer, "is_blocked", None) is not False:
        raise WithdrawalError("Аккаунт заблокирован или его статус неизвестен. Обратитесь в Playerok.")
    balance = await client.balance()
    value = amount(balance.get("withdrawable")) if isinstance(balance, dict) else None
    if value is None or value < 0 or value > Decimal("999999999999.99"):
        raise WithdrawalError("Playerok не вернул корректную доступную к выводу сумму.")
    return value

async def create_withdrawal(
    client: Any,
    own_id: str,
    *,
    provider_id: str,
    account: str,
    value: Any,
    payment_method_id: str | None = None,
    sbp_bank_member_id: str | None = None,
) -> dict[str, Any]:
    """Validate everything again and only then send the payout mutation."""
    identifier = str(provider_id or "").strip()
    if identifier not in TransactionProviderIds.__members__:
        raise WithdrawalError("Неизвестный способ вывода.")
    provider_enum = TransactionProviderIds[identifier]

    providers = await client.call("get_transaction_providers", TransactionProviderDirections.OUT)
    provider = next(
        (entry for entry in providers or [] if enum_name(getattr(entry, "id", None)) == identifier), None,
    )
    if provider is None:
        raise WithdrawalError("Playerok сейчас не предлагает этот способ вывода.")

    kind = requisite_kind(identifier)
    requisites = parse_requisites(kind, account)
    rubles = parse_amount(value)

    limits = getattr(getattr(provider, "limits", None), "outgoing", None)
    minimum = amount(getattr(limits, "min", None))
    maximum = amount(getattr(limits, "max", None))
    if minimum is not None and rubles < minimum:
        raise WithdrawalError(f"Минимальная сумма для этого способа — {money(minimum)}.")
    if maximum is not None and rubles > maximum:
        raise WithdrawalError(f"Максимальная сумма для этого способа — {money(maximum)}.")

    method_enum = None
    available_methods = {
        enum_name(getattr(method, "id", None)) for method in (getattr(provider, "payment_methods", None) or [])
    }
    if available_methods:
        chosen = str(payment_method_id or "").strip()
        if not chosen:
            # Only auto-pick when Playerok offers a single method: fees differ.
            if len(available_methods) != 1:
                raise WithdrawalError("Выберите платёжную систему карты.")
            chosen = next(iter(available_methods))
        if chosen not in available_methods or chosen not in TransactionPaymentMethodIds.__members__:
            raise WithdrawalError("Выберите способ оплаты, который предлагает Playerok.")
        method_enum = TransactionPaymentMethodIds[chosen]
    elif payment_method_id:
        raise WithdrawalError("Для этого способа вывода не нужно выбирать банк карты.")

    bank_id = None
    if identifier == "SBP":
        bank_id = str(sbp_bank_member_id or "").strip()
        banks = await sbp_banks(client)
        if not any(bank["id"] == bank_id for bank in banks):
            raise WithdrawalError("Выберите банк из списка СБП.")
    elif sbp_bank_member_id:
        raise WithdrawalError("Банк СБП указывается только для СБП.")

    masked = mask_requisites(kind, requisites)
    if kind == "card_id":
        cards = await verified_cards(client)
        selected = next((card for card in cards if card["id"] == requisites), None)
        if selected is None:
            raise WithdrawalError("Карта не найдена среди привязанных. Добавьте её на Playerok.")
        masked = f"•••• {selected['lastFour']}"

    available = await withdrawable(client, own_id)
    if rubles > available:
        raise WithdrawalError(f"Недостаточно средств. Доступно к выводу: {money(available)}.")

    try:
        transaction = await client.call(
            "request_withdrawal",
            provider=provider_enum,
            account=requisites,
            value=rubles,
            payment_method_id=method_enum,
            sbp_bank_member_id=bank_id,
        )
    except Exception as exc:
        # «Защита средств» requires an e-mail code the bot cannot fetch.
        if re.search(r"(?i)confirm|подтвержд|\bкод\b|code", playerok_error_text(exc)):
            raise WithdrawalError(
                "Playerok запросил код подтверждения из почты: включена «Защита средств». "
                "Отключите её в профиле Playerok или оформите выплату на сайте — "
                f"{PLAYEROK_WALLET_URL}"
            ) from exc
        raise
    result = payout_json(transaction)
    result["masked"] = masked
    result["requested"] = str(rubles)
    result["remaining"] = format((available - rubles).quantize(Decimal("0.01")), "f")
    return result
