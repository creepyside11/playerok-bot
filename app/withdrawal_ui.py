"""Telegram flow that creates a real Playerok payout.

The seller picks a payout method, fills requisites, enters an amount and
confirms. Every step re-reads the live provider catalogue, so a stale keyboard
can never send a payout to a provider Playerok no longer offers.
"""

from __future__ import annotations

import html
import time
import uuid
from decimal import Decimal
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .handlers import active_account, edit, svc
from .models import PlayerokAccount
from .playerok_errors import playerok_error_text
from .states import WithdrawalCreate
from .withdrawal_history import PLAYEROK_CONTACTS_URL, PLAYEROK_URL
from .withdrawal_request import (
    CARD_BINDING_HINT, PLAYEROK_WALLET_URL, WithdrawalError, create_withdrawal, mask_requisites,
    money, parse_amount, parse_requisites, requisite_kind, withdrawal_methods, withdrawable,
)

router = Router(name="withdrawal")
LIFETIME = 10 * 60
MAX_BANK_BUTTONS = 10

def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="balance:withdrawals")],
    ])

def wallet_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Кошелёк на Playerok", url=PLAYEROK_WALLET_URL)],
        [InlineKeyboardButton(text="⬅️ К истории вывода", callback_data="balance:withdrawals")],
    ])

def provider_title(provider: dict[str, Any]) -> str:
    fee = provider.get("fee")
    return f"{provider['name']} · {fee}%" if fee is not None else str(provider["name"])

def card_label(card: dict[str, Any]) -> str:
    mark = "✅ " if card.get("chosen") else ""
    return f"{mark}•••• {card['lastFour']} · {card.get('type') or 'карта'}"

def decimals(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return parsed if parsed.is_finite() else None

async def ensure_account(tg_id: int) -> PlayerokAccount | None:
    account = await active_account(tg_id)
    return account if account and account.tg_user_id == tg_id else None

async def fresh_state(tg_id: int, state: FSMContext) -> tuple[PlayerokAccount, dict[str, Any]]:
    data = await state.get_data()
    account = await ensure_account(tg_id)
    created = data.get("wd_created")
    if (
        not account
        or str(account.id) != data.get("wd_account")
        or not isinstance(created, (int, float))
        or not 0 <= time.monotonic() - created <= LIFETIME
    ):
        await state.clear()
        raise WithdrawalError("Заявка устарела. Начните оформление заново.")
    return account, data

def fail_text(exc: Exception) -> str:
    if isinstance(exc, WithdrawalError):
        return f"❌ {html.escape(str(exc))}"
    return f"❌ {html.escape(playerok_error_text(exc))}"

async def load_methods(call: CallbackQuery, state: FSMContext) -> tuple[PlayerokAccount, dict[str, Any], dict[str, Any]]:
    account, data = await fresh_state(call.from_user.id, state)
    client = await svc().gateway.get_client(account)
    return account, data, await withdrawal_methods(client)

@router.callback_query(F.data == "wd:start")
async def start(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    account = await ensure_account(call.from_user.id)
    if not account:
        await call.answer("Сначала подключите свой аккаунт.", show_alert=True)
        return
    await state.clear()
    await call.answer("Загружаю способы вывода…")
    try:
        client = await svc().gateway.get_client(account)
        methods = await withdrawal_methods(client)
        probe = getattr(client, "funds_protection", None)
        protection = bool(await probe()) if callable(probe) else False
    except Exception as exc:
        await edit(call, fail_text(exc), wallet_markup())
        return
    providers = methods["providers"]
    if not providers:
        await edit(call, (
            "Playerok сейчас не предлагает доступных способов вывода для этого аккаунта.\n\n"
            "Проверьте статус аккаунта и способы вывода на сайте."
        ), wallet_markup())
        return
    await state.set_data({"wd_account": str(account.id), "wd_created": time.monotonic()})
    await state.set_state(WithdrawalCreate.confirm)
    rows = [
        [InlineKeyboardButton(text=provider_title(provider), callback_data=f"wd:provider:{provider['id']}")]
        for provider in providers
    ]
    rows.append([InlineKeyboardButton(text="⬅️ История вывода", callback_data="balance:withdrawals")])
    warning = (
        "⚠️ В профиле включена «Защита средств»: Playerok потребует код из почты на новые "
        "реквизиты. Код вводится только на сайте.\n\n"
        if protection else ""
    )
    await edit(call, (
        f"💸 <b>Новая выплата · @{html.escape(account.username)}</b>\n\n"
        "Выберите способ вывода. Дальше бот попросит реквизиты, сумму и подтверждение.\n\n"
        f"{warning}"
        "<i>Реквизиты используются только для этой выплаты и не сохраняются.</i>"
    ), InlineKeyboardMarkup(inline_keyboard=rows))

async def ask_requisites(call: CallbackQuery, state: FSMContext, kind: str, title: str) -> None:
    prompts = {
        "card_number": "Введите номер карты (13–19 цифр). Сообщение будет удалено после обработки.",
        "phone": "Введите номер телефона для выплаты, например +7 999 123-45-67. Сообщение будет удалено.",
        "wallet": "Введите адрес кошелька USDT TRC20 (34 символа, начинается с T). Сообщение будет удалено.",
        "account": "Введите номер счёта для выплаты. Сообщение будет удалено.",
    }
    await state.set_state(WithdrawalCreate.requisites)
    await edit(call, (
        f"💸 <b>{html.escape(title)}</b>\n\n{html.escape(prompts.get(kind, prompts['account']))}\n\n"
        "Не отправляйте CVV, PIN или коды подтверждения — они не нужны."
    ), cancel_markup())

async def ask_amount(call: CallbackQuery, state: FSMContext) -> None:
    try:
        account, data = await fresh_state(call.from_user.id, state)
        client = await svc().gateway.get_client(account)
        available = await withdrawable(client, account.playerok_user_id)
    except Exception as exc:
        await edit(call, fail_text(exc), cancel_markup())
        return
    await state.update_data(wd_available=str(available))
    await state.set_state(WithdrawalCreate.amount)
    await edit(call, amount_prompt(data, available), cancel_markup())

def amount_prompt(data: dict[str, Any], available: Decimal) -> str:
    minimum, maximum = decimals(data.get("wd_min")), decimals(data.get("wd_max"))
    limit = ""
    if minimum is not None or maximum is not None:
        low = money(minimum) if minimum is not None else "—"
        high = money(maximum) if maximum is not None else "—"
        limit = f"Лимит способа: {low} – {high}\n"
    return (
        f"💸 <b>{html.escape(data.get('wd_title') or 'Вывод')}</b>\n\n"
        f"Доступно к выводу: <b>{money(available)}</b>\n{limit}"
        "Введите сумму целыми рублями, например 500."
    )

async def show_requisites_step(call: CallbackQuery, state: FSMContext, data: dict[str, Any], methods: dict[str, Any]) -> None:
    kind = data.get("wd_kind") or "account"
    title = str(data.get("wd_title") or "Вывод")
    if kind == "card_id":
        cards = methods["cards"]
        if not cards:
            await state.clear()
            await edit(call, (
                "💸 <b>Банковская карта РФ</b>\n\n"
                "У аккаунта нет привязанных карт.\n\n"
                f"{html.escape(CARD_BINDING_HINT)}"
            ), wallet_markup())
            return
        rows = [
            [InlineKeyboardButton(text=card_label(card), callback_data=f"wd:card:{card['id']}")]
            for card in cards
        ]
        rows.append([InlineKeyboardButton(text="🌐 Добавить карту на Playerok", url=PLAYEROK_WALLET_URL)])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="balance:withdrawals")])
        await edit(call, (
            f"💸 <b>{html.escape(title)}</b>\n\nВыберите привязанную карту.\n\n"
            f"<i>{html.escape(CARD_BINDING_HINT)}</i>"
        ), InlineKeyboardMarkup(inline_keyboard=rows))
        return
    if data.get("wd_needs_bank"):
        banks = methods["banks"]
        if not banks:
            await edit(call, "❌ Playerok не вернул список банков СБП. Попробуйте позже.", wallet_markup())
            return
        rows = [
            [InlineKeyboardButton(text=bank["name"], callback_data=f"wd:bank:{bank['id']}")]
            for bank in banks[:MAX_BANK_BUTTONS]
        ]
        if len(banks) > MAX_BANK_BUTTONS:
            rows.append([InlineKeyboardButton(text="🔍 Найти банк", callback_data="wd:bank_search")])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="balance:withdrawals")])
        await edit(call, (
            f"💸 <b>{html.escape(title)}</b>\n\nВыберите банк для перевода по СБП."
        ), InlineKeyboardMarkup(inline_keyboard=rows))
        return
    await ask_requisites(call, state, kind, title)

@router.callback_query(F.data.startswith("wd:provider:"))
async def choose_provider(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    provider_id = (call.data or "").rsplit(":", 1)[-1]
    await call.answer("Готовлю реквизиты…")
    try:
        _, _, methods = await load_methods(call, state)
    except Exception as exc:
        await edit(call, fail_text(exc), cancel_markup())
        return
    provider = next((entry for entry in methods["providers"] if entry["id"] == provider_id), None)
    if provider is None:
        await edit(call, "❌ Playerok больше не предлагает этот способ вывода.", wallet_markup())
        return
    await state.update_data(
        wd_provider=provider["id"],
        wd_kind=requisite_kind(provider["id"]),
        wd_title=provider["name"],
        wd_fee=str(provider["fee"]) if provider["fee"] is not None else "",
        wd_min_fee=str(provider["minFee"]) if provider["minFee"] is not None else "",
        wd_min=str(provider["minValue"]) if provider["minValue"] is not None else "",
        wd_max=str(provider["maxValue"]) if provider["maxValue"] is not None else "",
        wd_needs_bank=bool(provider["needsBank"]),
        wd_method=None,
        wd_bank=None,
        wd_bank_name=None,
        wd_requisites=None,
        wd_masked=None,
    )
    methods_list = provider["paymentMethods"]
    if len(methods_list) > 1:
        rows = [
            [InlineKeyboardButton(text=entry["name"] or entry["id"], callback_data=f"wd:method:{entry['id']}")]
            for entry in methods_list
        ]
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="balance:withdrawals")])
        await state.set_state(WithdrawalCreate.confirm)
        await edit(call, (
            f"💸 <b>{html.escape(provider['name'])}</b>\n\n"
            "Выберите платёжную систему карты — от неё зависит комиссия."
        ), InlineKeyboardMarkup(inline_keyboard=rows))
        return
    await state.update_data(wd_method=methods_list[0]["id"] if methods_list else None)
    data = await state.get_data()
    await state.set_state(WithdrawalCreate.confirm)
    await show_requisites_step(call, state, data, methods)

@router.callback_query(F.data.startswith("wd:method:"))
async def choose_method(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    method_id = (call.data or "").rsplit(":", 1)[-1]
    await call.answer()
    try:
        _, data, methods = await load_methods(call, state)
    except Exception as exc:
        await edit(call, fail_text(exc), cancel_markup())
        return
    provider = next((entry for entry in methods["providers"] if entry["id"] == data.get("wd_provider")), None)
    allowed = {entry["id"] for entry in (provider or {}).get("paymentMethods", [])}
    if method_id not in allowed:
        await edit(call, "❌ Playerok не предлагает эту платёжную систему.", wallet_markup())
        return
    await state.update_data(wd_method=method_id)
    data = await state.get_data()
    await state.set_state(WithdrawalCreate.confirm)
    await show_requisites_step(call, state, data, methods)

@router.callback_query(F.data == "wd:bank_search")
async def bank_search(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    await call.answer()
    await state.set_state(WithdrawalCreate.bank)
    await edit(call, (
        "🔍 <b>Поиск банка СБП</b>\n\nОтправьте название банка, например «Сбер» или «Тинькофф»."
    ), cancel_markup())

@router.message(WithdrawalCreate.bank, F.chat.type == ChatType.PRIVATE)
async def bank_query(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip().lower()[:40]
    if len(query) < 2:
        await message.answer("Введите хотя бы два символа названия банка.", reply_markup=cancel_markup())
        return
    try:
        account, _ = await fresh_state(message.from_user.id, state)
        client = await svc().gateway.get_client(account)
        methods = await withdrawal_methods(client)
    except Exception as exc:
        await message.answer(fail_text(exc), reply_markup=wallet_markup())
        return
    matches = [bank for bank in methods["banks"] if query in bank["name"].lower()][:MAX_BANK_BUTTONS]
    if not matches:
        await message.answer("Банк не найден. Попробуйте другое название.", reply_markup=cancel_markup())
        return
    rows = [
        [InlineKeyboardButton(text=bank["name"], callback_data=f"wd:bank:{bank['id']}")]
        for bank in matches
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="balance:withdrawals")])
    await message.answer("Выберите банк:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data.startswith("wd:bank:"))
async def choose_bank(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    bank_id = (call.data or "").rsplit(":", 1)[-1]
    await call.answer()
    try:
        _, data, methods = await load_methods(call, state)
    except Exception as exc:
        await edit(call, fail_text(exc), cancel_markup())
        return
    bank = next((entry for entry in methods["banks"] if entry["id"] == bank_id), None)
    if bank is None:
        await edit(call, "❌ Банк не найден в списке СБП. Попробуйте снова.", cancel_markup())
        return
    await state.update_data(wd_bank=bank["id"], wd_bank_name=bank["name"])
    data = await state.get_data()
    await state.set_state(WithdrawalCreate.confirm)
    await ask_requisites(call, state, data.get("wd_kind") or "phone", data.get("wd_title") or "СБП")

@router.callback_query(F.data.startswith("wd:card:"))
async def choose_card(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    card_id = (call.data or "").rsplit(":", 1)[-1]
    await call.answer()
    try:
        _, _, methods = await load_methods(call, state)
    except Exception as exc:
        await edit(call, fail_text(exc), cancel_markup())
        return
    card = next((entry for entry in methods["cards"] if entry["id"] == card_id), None)
    if card is None:
        await edit(call, "❌ Карта не найдена среди привязанных. Добавьте её на Playerok.", wallet_markup())
        return
    await state.update_data(wd_requisites=card["id"], wd_masked=f"•••• {card['lastFour']}")
    await ask_amount(call, state)

@router.message(WithdrawalCreate.requisites, F.chat.type == ChatType.PRIVATE)
async def input_requisites(message: Message, state: FSMContext) -> None:
    # Best effort: the requisites must not stay in the chat history.
    try:
        await message.delete()
    except Exception:
        pass
    try:
        account, data = await fresh_state(message.from_user.id, state)
        kind = data.get("wd_kind") or "account"
        value = parse_requisites(kind, message.text)
    except WithdrawalError as exc:
        await message.answer(f"❌ {html.escape(str(exc))}", reply_markup=cancel_markup())
        return
    except Exception as exc:
        await message.answer(fail_text(exc), reply_markup=wallet_markup())
        return
    await state.update_data(wd_requisites=value, wd_masked=mask_requisites(kind, value))
    try:
        client = await svc().gateway.get_client(account)
        available = await withdrawable(client, account.playerok_user_id)
    except Exception as exc:
        await message.answer(fail_text(exc), reply_markup=cancel_markup())
        return
    await state.update_data(wd_available=str(available))
    await state.set_state(WithdrawalCreate.amount)
    await message.answer(
        amount_prompt(data, available), reply_markup=cancel_markup(), parse_mode="HTML",
    )

@router.message(WithdrawalCreate.amount, F.chat.type == ChatType.PRIVATE)
async def input_amount(message: Message, state: FSMContext) -> None:
    try:
        rubles = parse_amount(message.text)
        account, data = await fresh_state(message.from_user.id, state)
        client = await svc().gateway.get_client(account)
        available = await withdrawable(client, account.playerok_user_id)
    except WithdrawalError as exc:
        await message.answer(f"❌ {html.escape(str(exc))}", reply_markup=cancel_markup())
        return
    except Exception as exc:
        await message.answer(fail_text(exc), reply_markup=cancel_markup())
        return
    if rubles > available:
        await message.answer(
            f"Недостаточно средств. Доступно к выводу: {money(available)}. Введите меньшую сумму.",
            reply_markup=cancel_markup(),
        )
        return
    minimum, maximum = decimals(data.get("wd_min")), decimals(data.get("wd_max"))
    if minimum is not None and rubles < minimum:
        await message.answer(
            f"Минимальная сумма для этого способа — {money(minimum)}.", reply_markup=cancel_markup(),
        )
        return
    if maximum is not None and rubles > maximum:
        await message.answer(
            f"Максимальная сумма для этого способа — {money(maximum)}.", reply_markup=cancel_markup(),
        )
        return
    nonce = uuid.uuid4().hex[:16]
    await state.update_data(wd_amount=str(rubles), wd_nonce=nonce, wd_available=str(available))
    await state.set_state(WithdrawalCreate.confirm)
    await message.answer(
        confirmation_text(data, rubles, available),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить выплату", callback_data=f"wd:confirm:{nonce}")],
            [InlineKeyboardButton(text="✏️ Изменить сумму", callback_data="wd:amount")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="balance:withdrawals")],
        ]),
        parse_mode="HTML",
    )

def confirmation_text(data: dict[str, Any], rubles: int, available: Decimal) -> str:
    percent = decimals(data.get("wd_fee")) or Decimal(0)
    minimum = decimals(data.get("wd_min_fee"))
    fee = Decimal(rubles) * percent / 100
    if minimum is not None and fee < minimum:
        fee = minimum
    payout = Decimal(rubles) - fee
    title = html.escape(str(data.get("wd_title") or "Вывод"))
    bank = data.get("wd_bank_name")
    method = f"{title} · {html.escape(str(bank))}" if bank else title
    floor = f", но не меньше {money(minimum)}" if minimum is not None and minimum > 0 else ""
    return (
        "💸 <b>Подтверждение выплаты</b>\n\n"
        f"Способ: <b>{method}</b>\n"
        f"Реквизиты: <b>{html.escape(str(data.get('wd_masked') or '—'))}</b>\n"
        f"Сумма: <b>{money(rubles)}</b>\n"
        f"Комиссия: <b>{money(fee)}</b> ({percent}%{floor})\n"
        f"К получению: <b>{money(payout)}</b>\n"
        f"Остаток к выводу: <b>{money(available - Decimal(rubles))}</b>\n\n"
        "Нажмите «Подтвердить», чтобы Playerok создал заявку на выплату. "
        "Отменить её в боте нельзя — только на сайте Playerok, пока она в обработке."
    )

@router.callback_query(F.data == "wd:amount")
async def change_amount(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    await call.answer()
    try:
        await fresh_state(call.from_user.id, state)
    except Exception as exc:
        await edit(call, fail_text(exc), cancel_markup())
        return
    await state.update_data(wd_nonce=None)
    await ask_amount(call, state)

@router.callback_query(F.data.startswith("wd:confirm:"))
async def confirm(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    data = await state.get_data()
    if (
        await state.get_state() != WithdrawalCreate.confirm.state
        or call.data.rsplit(":", 1)[-1] != data.get("wd_nonce")
        or not data.get("wd_requisites")
    ):
        await call.answer("Эта заявка уже отправлена или устарела.", show_alert=True)
        return
    await call.answer("Отправляю заявку в Playerok…")
    payload = dict(data)
    # Clear first: a repeated tap must never create a second payout.
    await state.clear()
    try:
        account = await ensure_account(call.from_user.id)
        if account is None or str(account.id) != payload.get("wd_account"):
            raise WithdrawalError("Аккаунт изменился. Начните оформление заново.")
        client = await svc().gateway.get_client(account)
        result = await create_withdrawal(
            client,
            account.playerok_user_id,
            provider_id=payload.get("wd_provider") or "",
            account=payload.get("wd_requisites") or "",
            value=payload.get("wd_amount"),
            payment_method_id=payload.get("wd_method"),
            sbp_bank_member_id=payload.get("wd_bank"),
        )
    except Exception as exc:
        await edit(call, fail_text(exc), wallet_markup())
        return
    await edit(call, (
        "✅ <b>Заявка на выплату создана</b>\n\n"
        f"Сумма: <b>{money(result.get('amount'))}</b>\n"
        f"Статус: <b>{html.escape(str(result.get('statusLabel') or '—'))}</b>\n"
        f"Реквизиты: <b>{html.escape(str(result.get('masked') or '—'))}</b>\n"
        f"Комиссия: <b>{money(result.get('fee'))}</b>\n\n"
        f"Остаток к выводу: <b>{money(result.get('remaining'))}</b>\n\n"
        "Статус обновляется на стороне Playerok. Если выплата не пришла, напишите в поддержку."
    ), InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 История вывода", callback_data="balance:withdrawals")],
        [InlineKeyboardButton(text="🌐 Открыть Playerok", url=PLAYEROK_URL)],
        [InlineKeyboardButton(text="Помощь с выплатами", url=PLAYEROK_CONTACTS_URL)],
    ]))
