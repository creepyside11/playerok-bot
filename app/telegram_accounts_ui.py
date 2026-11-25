from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import html
from io import BytesIO
import logging
import re
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select
from playerokapi.enums import ItemDealDirections, ItemDealStatuses

from .advanced_handlers import plugins
from .handlers import active_account, clip, edit, require_account, svc
from .keyboards import back_menu
from .models import PlayerokAccount
from .playerok_errors import playerok_error_text
from .states import TelegramAccountsState
from .telegram_accounts_manager import (
    COUNTRY_PRESETS,
    default_telegram_api_hash,
    default_telegram_api_id,
    TelegramAccountDeal,
    TelegramAccountLotRule,
    TelegramAccountStock,
    TelegramAccountsConfig,
    available_stock_query,
    bind_keyword,
    bound_lot_rule,
    valid_keyword,
)
from .telegram_session_file import MAX_SESSION_BYTES, SessionFileError, verify_session_file

logger = logging.getLogger("telegram_accounts_ui")
router = Router(name="telegram_accounts_ui")

# In-memory dictionary for pending interactive Telethon logins
# key: tg_user_id -> dict(client, phone, hash, country, account_id)
PENDING_PHONE_AUTHS: dict[int, dict[str, Any]] = {}
LOGIN_CODE_CHANNELS = {
    "SentCodeTypeApp": "Telegram указал доставку в служебный чат приложения на уже авторизованном устройстве.",
    "SentCodeTypeSms": "Telegram указал доставку по SMS.",
    "SentCodeTypeSmsWord": "Telegram указал доставку слова по SMS.",
    "SentCodeTypeSmsPhrase": "Telegram указал доставку фразы по SMS.",
    "SentCodeTypeEmailCode": "Telegram указал доставку на привязанную почту.",
    "SentCodeTypeCall": "Telegram продиктует код по телефону.",
}


async def cleanup_pending_auth(tg_user_id: int) -> None:
    data = PENDING_PHONE_AUTHS.pop(tg_user_id, None)
    if data and "client" in data:
        client = data["client"]
        try:
            if hasattr(client, "is_connected") and client.is_connected():
                await client.disconnect()
        except Exception:
            pass


async def get_or_create_config(account_id: Any) -> TelegramAccountsConfig:
    async with svc().db() as session:
        cfg = await session.scalar(
            select(TelegramAccountsConfig).where(TelegramAccountsConfig.account_id == account_id)
        )
        if not cfg:
            cfg = TelegramAccountsConfig(account_id=account_id)
            session.add(cfg)
            await session.commit()
            await session.refresh(cfg)
        return cfg


# --- Главное меню Telegram Accounts ---

@router.callback_query(F.data == "tgacc:open")
async def tgacc_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        total_stocks = await session.scalar(
            select(func.count(TelegramAccountStock.id)).where(TelegramAccountStock.account_id == account.id)
        ) or 0
        avail_stocks = await session.scalar(
            select(func.count(TelegramAccountStock.id)).where(
                TelegramAccountStock.account_id == account.id,
                TelegramAccountStock.status == "available",
            )
        ) or 0
        issued_stocks = await session.scalar(
            select(func.count(TelegramAccountStock.id)).where(
                TelegramAccountStock.account_id == account.id,
                TelegramAccountStock.status == "issued",
            )
        ) or 0
        total_rules = await session.scalar(
            select(func.count(TelegramAccountLotRule.id)).where(TelegramAccountLotRule.account_id == account.id)
        ) or 0

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Менеджер аккаунтов (Склад)", callback_data="tgacc:manager")],
        [InlineKeyboardButton(text="➕ Привязать лот к стране", callback_data="tgacc:lots")],
        [InlineKeyboardButton(text="🔤 Привязать по слову в названии", callback_data="tgacc:keyword")],
        [InlineKeyboardButton(text=f"📋 Привязанные лоты ({total_rules})", callback_data="tgacc:rules")],
        [InlineKeyboardButton(text="🔍 Проверить автовыдачу сделки", callback_data="tgacc:diagnose")],
        [InlineKeyboardButton(text="⚙️ Настройки API (Telethon)", callback_data="tgacc:settings")],
        [InlineKeyboardButton(text="⬅️ Меню плагинов", callback_data="plugins:mine")],
    ])

    text = (
        "📱 <b>Telegram Accounts — Автовыдача</b>\n\n"
        "Плагин для автоматической выдачи профилей Telegram по странам.\n\n"
        "<b>Статистика:</b>\n"
        f"• Доступно к выдаче: <b>{avail_stocks}</b> шт.\n"
        f"• Всего выдано: <b>{issued_stocks}</b> шт.\n"
        f"• Всего в базе: <b>{total_stocks}</b> шт.\n"
        f"• Привязанных лотов: <b>{total_rules}</b> шт.\n\n"
        "<i>После оплаты покупателю отправляется номер телефона и команда #код. "
        "По команде #код бот получает проверочный код через Telethon и переводит сделку в статус «Выполнен».</i>"
    )
    await call.answer()
    await edit(call, text, markup)


@router.callback_query(F.data == "tgacc:diagnose")
async def tgacc_diagnose_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.clear()
    await state.set_state(TelegramAccountsState.diagnose_deal)
    await state.update_data(diagnostic_account_id=str(account.id))
    await call.answer()
    await edit(call, "Пришлите ID сделки или ссылку на неё: playerok.com/deal/…", back_menu("tgacc:open"))


@router.message(TelegramAccountsState.diagnose_deal, F.text, ~F.text.startswith("/"))
async def tgacc_diagnose_result(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    account = await active_account(message.from_user.id)
    if not account or data.get("diagnostic_account_id") != str(account.id):
        await state.clear()
        await message.answer("Откройте проверку для выбранного аккаунта.", reply_markup=back_menu("tgacc:open"))
        return
    match = re.fullmatch(
        r"(?:(?:https://)?(?:www\.)?playerok\.com/deal/)?"
        r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?:[/?#].*)?",
        message.text.strip(),
    )
    if not match:
        await message.answer("Введите ID сделки или ссылку из Playerok.")
        return
    deal_id = match[1].lower()
    try:
        client = await svc().gateway.get_client(account)
        deal = await client.get_deal(deal_id)
    except Exception as exc:
        await message.answer(html.escape(playerok_error_text(exc)), parse_mode="HTML", reply_markup=back_menu("tgacc:open"))
        return
    item = getattr(deal, "item", None)
    seller_id = getattr(getattr(item, "user", None), "id", None)
    if (str(getattr(deal, "id", "")) != deal_id or getattr(deal, "direction", None) != ItemDealDirections.OUT
            or seller_id and str(seller_id) != account.playerok_user_id):
        await message.answer("Playerok не подтвердил, что это продажа выбранного аккаунта.", reply_markup=back_menu("tgacc:open"))
        return
    report = await describe_deal_delivery(account, deal)
    await state.clear()
    await message.answer(report, parse_mode="HTML", reply_markup=back_menu("tgacc:open"))


async def describe_deal_delivery(account: PlayerokAccount, deal: Any) -> str:
    manager = plugins()
    spec = manager.plugins.get("telegram_accounts")
    enabled = bool(spec and (await manager.resolved_state(account.id, spec))[0])
    lot_id = str(getattr(getattr(deal, "item", None), "id", "") or "")
    chat = bool(getattr(getattr(deal, "chat", None), "id", None))
    status = getattr(deal, "status", None)
    async with svc().db() as session:
        rule = await bound_lot_rule(session, account.id, lot_id, getattr(getattr(deal, "item", None), "name", None) or "")
        delivery = await session.scalar(select(TelegramAccountDeal).where(
            TelegramAccountDeal.account_id == account.id, TelegramAccountDeal.deal_id == str(deal.id),
        ))
        stock = None
        if rule:
            available = await available_stock_query(session, account.id, rule.country)
            stock = await session.scalar(select(func.count()).select_from(available.subquery()))

    plugin_text = "Включён" if enabled else "Выключен" if spec else "Не загружен"
    lines = [
        f"<b>Сделка</b> <code>{html.escape(str(deal.id))}</code>",
        f"Статус Playerok — <code>{html.escape(getattr(status, 'name', None) or 'неизвестен')}</code>",
        f"ID лота из Playerok — <code>{html.escape(lot_id or 'не получен')}</code>",
        f"Чат сделки — {'есть' if chat else 'Playerok не вернул'}",
        f"Плагин — {plugin_text}",
    ]
    if rule:
        rule_text = "активна" if rule.enabled else "выключена"
        match = f"фраза «{html.escape(rule.keyword)}»" if rule.keyword else "ID лота"
        lines.append(f"Привязка — {rule_text}, {match}, {html.escape(rule.country)}. На складе — {stock}.")
    else:
        lines.append("Привязка — совпадений по ID и названию нет.")
    if delivery and delivery.intro_sent:
        lines.append("Отправка инструкции — подтверждена Playerok.")
    elif delivery:
        lines.append("Профиль зарезервирован. Отправка инструкции ещё не подтверждена.")
        if delivery.intro_error:
            lines.append(f"Последний сбой — <code>{html.escape(delivery.intro_error)}</code>")
    elif status in {ItemDealStatuses.PAID, ItemDealStatuses.PENDING} and enabled and rule and rule.enabled and chat and stock:
        lines.append("Условия выдачи выполнены. В базе пока нет записи о запуске выдачи по этой сделке.")
    else:
        lines.append("В базе нет записи о выдаче по этой сделке.")
    return "\n".join(lines)


# --- Менеджер аккаунтов / Склад ---

@router.callback_query(F.data == "tgacc:manager")
async def tgacc_manager_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        # Статистика по странам
        rows = list((await session.execute(
            select(
                TelegramAccountStock.country,
                TelegramAccountStock.status,
                func.count(TelegramAccountStock.id),
            )
            .where(TelegramAccountStock.account_id == account.id)
            .group_by(TelegramAccountStock.country, TelegramAccountStock.status)
        )).all())

    stats_by_country: dict[str, dict[str, int]] = {}
    for country, status, count in rows:
        if country not in stats_by_country:
            stats_by_country[country] = {"available": 0, "issued": 0, "error": 0}
        stats_by_country[country][status] = count

    breakdown_lines = []
    for country, counts in stats_by_country.items():
        avail = counts.get("available", 0)
        iss = counts.get("issued", 0)
        breakdown_lines.append(f"• <b>{html.escape(country)}</b>: {avail} доступно, {iss} выдано")

    breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "<i>На складе пока нет добавленных профилей.</i>"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить акк: Номер + Код (Telethon)", callback_data="tgacc:add:country")],
        [InlineKeyboardButton(text="📎 Добавить файл .session (Telethon)", callback_data="tgacc:add:file")],
        [InlineKeyboardButton(text="📝 Добавить готовую StringSession", callback_data="tgacc:add:raw_start")],
        [InlineKeyboardButton(text="📋 Список всех номеров", callback_data="tgacc:stock:list:0")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="tgacc:open")],
    ])

    text = (
        "👥 <b>Менеджер аккаунтов Telegram</b>\n\n"
        "<b>Наличие по странам:</b>\n"
        f"{breakdown_text}\n\n"
        "Выберите способ добавления профиля."
    )
    await call.answer()
    await edit(call, text, markup)


# --- Добавление: Выбор страны ---

@router.callback_query(F.data.in_({"tgacc:add:country", "tgacc:add:file"}))
async def tgacc_add_choose_country(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    session_file = call.data == "tgacc:add:file"
    if session_file and call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Импорт файла доступен в личном чате с ботом.", show_alert=True)
        return
    await cleanup_pending_auth(call.from_user.id)
    await state.clear()
    await state.update_data(stock_account_id=str(account.id), stock_session_file=session_file)

    b = InlineKeyboardBuilder()
    for name, prefix in COUNTRY_PRESETS:
        b.button(text=name, callback_data=f"tgacc:addc:{name}")
    b.button(text="🌐 Другая страна", callback_data="tgacc:addc:custom")
    b.button(text="❌ Отмена", callback_data="tgacc:manager")
    b.adjust(2, 2, 2, 2, 1, 1)

    await call.answer()
    await edit(
        call,
        "🌍 <b>Выберите страну для добавляемого профиля:</b>\n\n"
        "Страна будет закреплена за профилем для соответствия лотам.",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("tgacc:addc:"))
async def tgacc_add_country_selected(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("stock_account_id"):
        await call.answer("Откройте «Менеджер аккаунтов» и выберите способ добавления заново.", show_alert=True)
        return
    country = call.data.split(":", 2)[2]
    if country == "custom":
        await state.set_state(TelegramAccountsState.custom_country)
        await call.answer()
        await edit(
            call,
            "🌐 Введите название страны (например, <i>Польша</i> или <i>Турция</i>):",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:manager")]]),
        )
        return

    await state.update_data(chosen_country=country)
    session_file = data.get("stock_session_file")
    await state.set_state(TelegramAccountsState.add_session_file if session_file else TelegramAccountsState.add_phone)
    await call.answer()
    await edit(
        call,
        stock_entry_prompt(country, session_file),
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:manager")]]),
    )


def stock_entry_prompt(country: str, session_file: bool) -> str:
    entry = (
        "Пришлите файл Telethon <code>.session</code> документом. Бот проверит действующую сессию и получит номер из неё."
        if session_file else
        "Введите номер телефона в международном формате. Например, <code>+12025550123</code>."
    )
    return f"🌍 Страна: <b>{html.escape(country)}</b>\n\n{entry}"


@router.message(TelegramAccountsState.custom_country, F.text, ~F.text.startswith("/"))
async def tgacc_custom_country_entered(message: Message, state: FSMContext) -> None:
    country = (message.text or "").strip()
    if not 1 <= len(country) <= 128:
        await message.answer("Введите название страны длиной до 128 символов.")
        return
    session_file = (await state.get_data()).get("stock_session_file")
    await state.update_data(chosen_country=country)
    await state.set_state(TelegramAccountsState.add_session_file if session_file else TelegramAccountsState.add_phone)
    await message.answer(
        stock_entry_prompt(country, session_file),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:manager")]]),
        parse_mode="HTML",
    )


@router.message(TelegramAccountsState.add_phone)
async def tgacc_phone_entered(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    data = await state.get_data()
    if not account or data.get("stock_account_id") != str(account.id):
        await message.answer("Откройте добавление заново для выбранного аккаунта.", reply_markup=back_menu("tgacc:manager"))
        await state.clear()
        return

    raw_phone = (message.text or "").strip()
    clean_phone = re.sub(r"[^\d+]", "", raw_phone)
    if not clean_phone.startswith("+"):
        clean_phone = "+" + clean_phone

    if not re.match(r"^\+\d{7,16}$", clean_phone):
        await message.answer("❌ Некорректный формат номера. Введите номер с кодом страны, например: <code>+79991234567</code>", parse_mode="HTML")
        return

    country = data.get("chosen_country", "Другая")

    cfg = await get_or_create_config(account.id)
    api_id = cfg.api_id or default_telegram_api_id()
    api_hash = cfg.api_hash or default_telegram_api_hash()
    if not api_hash:
        await message.answer("Сначала задайте API ID и API Hash.", reply_markup=back_menu("tgacc:settings"))
        return

    status_msg = await message.answer("⏳ Подключаюсь к Telegram и отправляю код запроса…")

    client = None
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        res = await client.send_code_request(clean_phone)
    except Exception as exc:
        logger.exception("Telethon send_code_request error")
        await status_msg.edit_text(
            f"❌ <b>Ошибка отправки кода:</b>\n<code>{html.escape(str(exc))}</code>\n\n"
            "Проверьте номер или настройки API в меню настроек.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В менеджер", callback_data="tgacc:manager")]]),
            parse_mode="HTML",
        )
        if client is not None:
            with suppress(Exception):
                await client.disconnect()
        await state.clear()
        return

    channel = getattr(res, "type", None)
    channel_name = type(channel).__name__
    code_notice = LOGIN_CODE_CHANNELS.get(channel_name)
    if not code_notice or not getattr(res, "phone_code_hash", None):
        with suppress(Exception):
            await client.disconnect()
        await state.clear()
        await status_msg.edit_text(
            "Telegram выбрал способ подтверждения, который бот не может принять. Можно добавить готовую сессию.",
            reply_markup=back_menu("tgacc:add:file"),
        )
        return

    # Сохраняем активный клиент в памяти
    PENDING_PHONE_AUTHS[message.from_user.id] = {
        "client": client,
        "phone": clean_phone,
        "phone_code_hash": res.phone_code_hash,
        "country": country,
        "account_id": account.id,
        "text_code": channel_name in {"SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase"},
    }

    await state.set_state(TelegramAccountsState.add_code)
    await status_msg.edit_text(
        f"📨 <b>{code_notice}</b>\n\n"
        f"📱 Номер: <code>{html.escape(clean_phone)}</code>\n"
        f"🌍 Страна: <b>{html.escape(country)}</b>\n\n"
        "Введите код подтверждения.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:cancel_auth")]]),
        parse_mode="HTML",
    )


@router.message(TelegramAccountsState.add_session_file, F.chat.type == ChatType.PRIVATE, F.document)
async def tgacc_session_file_entered(message: Message, state: FSMContext, bot: Bot) -> None:
    account = await active_account(message.from_user.id)
    data = await state.get_data()
    country = data.get("chosen_country")
    if not account or data.get("stock_account_id") != str(account.id) or not country:
        await state.clear()
        await message.answer("Откройте импорт заново для выбранного аккаунта.", reply_markup=back_menu("tgacc:manager"))
        return

    document = message.document
    if not (document.file_name or "").casefold().endswith(".session"):
        await message.answer("Пришлите файл Telethon с расширением .session.")
        return
    if not document.file_size or document.file_size > MAX_SESSION_BYTES:
        await message.answer("Пришлите непустой файл .session размером до 8 МБ.")
        return
    cfg = await get_or_create_config(account.id)
    api_hash = cfg.api_hash or default_telegram_api_hash()
    if not api_hash:
        await message.answer("Сначала задайте API ID и API Hash.", reply_markup=back_menu("tgacc:settings"))
        return
    api_id = cfg.api_id if cfg.api_hash else default_telegram_api_id()

    status = await message.answer("Проверяю сессию в Telegram…")
    try:
        with BytesIO() as buffer:
            await bot.download(document, destination=buffer, timeout=25)
            contents = buffer.getvalue()
        phone, session_string = await verify_session_file(contents, api_id, api_hash)
    except SessionFileError as exc:
        await status.edit_text(str(exc), reply_markup=back_menu("tgacc:manager"))
        return
    except Exception as exc:
        logger.warning("Telegram session file import failed (%s)", type(exc).__name__)
        await status.edit_text(
            "Не удалось скачать файл или подключиться к Telegram. Попробуйте ещё раз.",
            reply_markup=back_menu("tgacc:manager"),
        )
        return

    async with svc().db() as session:
        seller = await session.get(PlayerokAccount, account.id, with_for_update=True)
        if seller is None or seller.tg_user_id != message.from_user.id:
            await state.clear()
            await status.edit_text("Аккаунт Playerok больше недоступен.", reply_markup=back_menu("tgacc:manager"))
            return
        existing = await session.scalar(select(TelegramAccountStock.id).where(
            TelegramAccountStock.account_id == account.id,
            TelegramAccountStock.phone.in_([phone, phone.lstrip("+")]),
        ).limit(1))
        if existing is not None:
            await status.edit_text("Этот номер уже есть на складе.", reply_markup=back_menu("tgacc:manager"))
            return
        session.add(TelegramAccountStock(
            account_id=account.id, phone=phone, country=country,
            session_encrypted=svc().cipher.encrypt(session_string),
        ))
        await session.commit()

    await state.clear()
    await status.edit_text(
        "Сессия проверена и добавлена на склад.\n\n"
        f"Номер: <code>{html.escape(phone)}</code>\nСтрана: <b>{html.escape(country)}</b>",
        reply_markup=back_menu("tgacc:manager"),
        parse_mode="HTML",
    )


@router.message(TelegramAccountsState.add_session_file, F.chat.type == ChatType.PRIVATE, ~F.document, ~F.text.startswith("/"))
async def tgacc_session_file_expected(message: Message) -> None:
    await message.answer("Пришлите .session как файл через «Документ».", reply_markup=back_menu("tgacc:manager"))


@router.callback_query(F.data == "tgacc:cancel_auth")
async def tgacc_cancel_auth(call: CallbackQuery, state: FSMContext) -> None:
    await cleanup_pending_auth(call.from_user.id)
    await state.clear()
    await call.answer("Отменено")
    await tgacc_manager_menu(call, state)


@router.message(TelegramAccountsState.add_code)
async def tgacc_code_entered(message: Message, state: FSMContext) -> None:
    auth_data = PENDING_PHONE_AUTHS.get(message.from_user.id)
    if not auth_data:
        await message.answer("Время ожидания истекло. Начните добавление заново.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В менеджер", callback_data="tgacc:manager")]]))
        await state.clear()
        return

    code_raw = (message.text or "").strip()
    clean_code = code_raw if auth_data.get("text_code") else re.sub(r"\D", "", code_raw)
    if not clean_code:
        await message.answer("Введите код подтверждения.")
        return

    client = auth_data["client"]
    phone = auth_data["phone"]
    phone_code_hash = auth_data["phone_code_hash"]
    country = auth_data["country"]
    account_id = auth_data["account_id"]

    try:
        from telethon.errors import SessionPasswordNeededError

        await client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        await state.set_state(TelegramAccountsState.add_password)
        await message.answer(
            "🔐 <b>Требуется пароль двухфакторной аутентификации (2FA):</b>\n\n"
            "На этом аккаунте установлен облачный пароль. Введите его сообщением:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:cancel_auth")]]),
            parse_mode="HTML",
        )
        return
    except Exception as exc:
        logger.exception("Sign in error")
        await message.answer(
            f"❌ <b>Ошибка входа:</b> <code>{html.escape(str(exc))}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В менеджер", callback_data="tgacc:manager")]]),
            parse_mode="HTML",
        )
        await cleanup_pending_auth(message.from_user.id)
        await state.clear()
        return

    # Успешный вход
    session_str = client.session.save()
    try:
        await client.disconnect()
    except Exception:
        pass
    PENDING_PHONE_AUTHS.pop(message.from_user.id, None)

    # Шифруем сессию
    cipher = svc().cipher
    session_enc = cipher.encrypt(session_str)

    async with svc().db() as session:
        stock = TelegramAccountStock(
            account_id=account_id,
            country=country,
            phone=phone,
            session_encrypted=session_enc,
            status="available",
            created_at=datetime.now(timezone.utc),
        )
        session.add(stock)
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>Профиль успешно добавлен!</b>\n\n"
        f"📱 Номер: <code>{html.escape(phone)}</code>\n"
        f"🌍 Страна: <b>{html.escape(country)}</b>\n"
        f"🟢 Статус: <b>Доступен к выдаче</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Менеджер аккаунтов", callback_data="tgacc:manager")],
            [InlineKeyboardButton(text="📱 Главное меню плагина", callback_data="tgacc:open")],
        ]),
        parse_mode="HTML",
    )


@router.message(TelegramAccountsState.add_password)
async def tgacc_password_entered(message: Message, state: FSMContext) -> None:
    auth_data = PENDING_PHONE_AUTHS.get(message.from_user.id)
    if not auth_data:
        await message.answer("Время ожидания истекло. Начните добавление заново.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В менеджер", callback_data="tgacc:manager")]]))
        await state.clear()
        return

    password = (message.text or "").strip()
    client = auth_data["client"]
    phone = auth_data["phone"]
    country = auth_data["country"]
    account_id = auth_data["account_id"]

    try:
        await client.sign_in(password=password)
    except Exception as exc:
        await message.answer(
            f"❌ <b>Неверный пароль или ошибка:</b> <code>{html.escape(str(exc))}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В менеджер", callback_data="tgacc:manager")]]),
            parse_mode="HTML",
        )
        await cleanup_pending_auth(message.from_user.id)
        await state.clear()
        return

    session_str = client.session.save()
    try:
        await client.disconnect()
    except Exception:
        pass
    PENDING_PHONE_AUTHS.pop(message.from_user.id, None)

    cipher = svc().cipher
    session_enc = cipher.encrypt(session_str)

    async with svc().db() as session:
        stock = TelegramAccountStock(
            account_id=account_id,
            country=country,
            phone=phone,
            session_encrypted=session_enc,
            status="available",
            created_at=datetime.now(timezone.utc),
        )
        session.add(stock)
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>Профиль успешно добавлен с 2FA!</b>\n\n"
        f"📱 Номер: <code>{html.escape(phone)}</code>\n"
        f"🌍 Страна: <b>{html.escape(country)}</b>\n"
        f"🟢 Статус: <b>Доступен к выдаче</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Менеджер аккаунтов", callback_data="tgacc:manager")],
            [InlineKeyboardButton(text="📱 Главное меню плагина", callback_data="tgacc:open")],
        ]),
        parse_mode="HTML",
    )


# --- Добавление готовой StringSession ---

@router.callback_query(F.data == "tgacc:add:raw_start")
async def tgacc_add_raw_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    b = InlineKeyboardBuilder()
    for name, _ in COUNTRY_PRESETS:
        b.button(text=name, callback_data=f"tgacc:rawc:{name}")
    b.button(text="❌ Отмена", callback_data="tgacc:manager")
    b.adjust(2)
    await call.answer()
    await edit(call, "🌍 Выберите страну для профиля:", b.as_markup())


@router.callback_query(F.data.startswith("tgacc:rawc:"))
async def tgacc_raw_country_selected(call: CallbackQuery, state: FSMContext) -> None:
    country = call.data.split(":", 2)[2]
    await state.update_data(chosen_country=country)
    await state.set_state(TelegramAccountsState.add_raw_session)
    await call.answer()
    await edit(
        call,
        f"🌍 Страна: <b>{html.escape(country)}</b>\n\n"
        "Отправьте сообщение в формате:\n"
        "<code>НОМЕР СЕССИЯ</code>\n\n"
        "<i>Пример:</i>\n"
        "<code>+79991234567 1BAAd7...StringSession...</code>",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:manager")]]),
    )


@router.message(TelegramAccountsState.add_raw_session)
async def tgacc_raw_session_entered(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        return
    text = (message.text or "").strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        await message.answer("❌ Формат: <code>+НОМЕР СТРОКА_СЕССИИ</code>", parse_mode="HTML")
        return

    phone, session_str = parts[0].strip(), parts[1].strip()
    data = await state.get_data()
    country = data.get("chosen_country", "Другая")

    # Проверяем валидность сессии
    cfg = await get_or_create_config(account.id)
    api_hash = cfg.api_hash or default_telegram_api_hash()
    if not api_hash:
        await message.answer("Сначала задайте API ID и API Hash.", reply_markup=back_menu("tgacc:settings"))
        return
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(session_str), cfg.api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            await message.answer("❌ Сессия не авторизована или отозвана.")
            return
        await client.disconnect()
    except Exception as exc:
        await message.answer(f"❌ Ошибка проверки сессии: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")
        return

    cipher = svc().cipher
    session_enc = cipher.encrypt(session_str)

    async with svc().db() as session:
        stock = TelegramAccountStock(
            account_id=account.id,
            country=country,
            phone=phone,
            session_encrypted=session_enc,
            status="available",
            created_at=datetime.now(timezone.utc),
        )
        session.add(stock)
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>Профиль успешно добавлен из StringSession!</b>\n\n"
        f"📱 Номер: <code>{html.escape(phone)}</code>\n"
        f"🌍 Страна: <b>{html.escape(country)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Менеджер аккаунтов", callback_data="tgacc:manager")],
        ]),
        parse_mode="HTML",
    )


# --- Список номеров на складе ---

@router.callback_query(F.data.startswith("tgacc:stock:list:"))
async def tgacc_stock_list_view(call: CallbackQuery, page: int | None = None) -> None:
    account = await require_account(call)
    if not account:
        return
    if page is None:
        page = int(call.data.split(":")[-1])

    async with svc().db() as session:
        total = await session.scalar(
            select(func.count(TelegramAccountStock.id)).where(TelegramAccountStock.account_id == account.id)
        ) or 0
        stocks = list((await session.scalars(
            select(TelegramAccountStock)
            .where(TelegramAccountStock.account_id == account.id)
            .order_by(TelegramAccountStock.id.desc())
            .offset(page * 6)
            .limit(6)
        )).all())

    page_size = 6
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    b = InlineKeyboardBuilder()
    for s in stocks:
        st_icon = "🟢" if s.status == "available" else "🔵" if s.status == "issued" else "🔴"
        b.button(text=f"{st_icon} {s.phone} ({s.country})", callback_data=f"tgacc:stock:view:{s.id}")
        b.button(text="🗑", callback_data=f"tgacc:stock:del:{s.id}:{page}")
    b.adjust(2)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"tgacc:stock:list:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"tgacc:stock:list:{page + 1}"))
    if nav:
        b.row(*nav)

    b.row(InlineKeyboardButton(text="⬅️ В менеджер", callback_data="tgacc:manager"))

    await call.answer()
    await edit(
        call,
        f"📋 <b>Список номеров на складе (всего: {total})</b>:\n"
        "🟢 — Доступен к выдаче\n"
        "🔵 — Выдан покупателю\n\n"
        "Нажмите 🗑 для удаления профиля из базы.",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("tgacc:stock:del:"))
async def tgacc_stock_delete(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    parts = call.data.split(":")
    stock_id = int(parts[3])
    page = int(parts[4])

    async with svc().db() as session:
        s = await session.get(TelegramAccountStock, stock_id)
        if s and s.account_id == account.id:
            await session.delete(s)
            await session.commit()

    await call.answer("Удалено")
    await tgacc_stock_list_view(call, page=page)


@router.callback_query(F.data.startswith("tgacc:stock:view:"))
async def tgacc_stock_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    stock_id = int(call.data.split(":")[-1])

    async with svc().db() as session:
        s = await session.get(TelegramAccountStock, stock_id)
        if not s or s.account_id != account.id:
            await call.answer("Не найден", show_alert=True)
            return

    st_label = "🟢 Доступен к выдаче" if s.status == "available" else f"🔵 Выдан (сделка {s.issued_deal_id or '—'})"
    text = (
        f"📱 <b>Информация о профиле</b>\n\n"
        f"Номер: <code>{html.escape(s.phone)}</code>\n"
        f"Страна: <b>{html.escape(s.country)}</b>\n"
        f"Статус: <b>{st_label}</b>\n"
        f"Добавлен: <code>{s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else '—'}</code>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"tgacc:stock:del:{s.id}:0")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="tgacc:stock:list:0")],
    ])
    await call.answer()
    await edit(call, text, markup)


# --- Привязка лотов и выбор страны ---

@router.callback_query(F.data == "tgacc:lots")
async def tgacc_lots_select(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    try:
        client = await svc().gateway.get_client(account)
        res = await client.call("get_my_items", statuses=None, count=24)
        all_items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        await call.answer(f"Ошибка загрузки лотов: {str(exc)[:100]}", show_alert=True)
        return

    if not all_items:
        await call.answer("У вас нет выставленных товаров", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    for item in all_items:
        b.button(text=f"📦 {clip(item.name, 26)}", callback_data=f"tgacc:lot:{item.id}")
    b.button(text="⬅️ Назад", callback_data="tgacc:open")
    b.adjust(1)

    await call.answer()
    await edit(
        call,
        "➕ <b>Выберите лот для привязки к Telegram:</b>\n\n"
        "После выбора лота вам будет предложено выбрать страну, которая будет автоматически выдаваться для этого товара.",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("tgacc:lot:"))
async def tgacc_lot_country_choose(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    lot_id = call.data.split(":", 2)[2]

    # Сохраняем выбранный лот в FSM состояние для избежания переполнения callback_data (лимит 64 байта в Telegram)
    await state.update_data(bind_lot_id=lot_id, bind_account_id=str(account.id), bind_keyword=None)

    # Получаем название лота
    try:
        client = await svc().gateway.get_client(account)
        item = await client.get_item(lot_id)
        lot_title = getattr(item, "name", f"Лот #{lot_id}")
    except Exception:
        lot_title = f"Лот #{lot_id}"

    await state.update_data(bind_lot_title=lot_title)

    await call.answer()
    await edit(
        call,
        f"📦 Лот: <b>{html.escape(lot_title)}</b>\n\n"
        "🌍 <b>Выберите страну, профиль которой нужно выдавать при покупке этого лота:</b>",
        bind_country_buttons(),
    )


def bind_country_buttons() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for idx, (name, _) in enumerate(COUNTRY_PRESETS):
        builder.button(text=name, callback_data=f"tgacc:bind:{idx}")
    builder.button(text="🌐 Другая страна", callback_data="tgacc:bind:custom")
    builder.button(text="❌ Отмена", callback_data="tgacc:open")
    builder.adjust(2, 2, 2, 2, 1, 1, 1)
    return builder.as_markup()


@router.callback_query(F.data == "tgacc:keyword")
async def tgacc_keyword_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.clear()
    await state.set_state(TelegramAccountsState.rule_keyword)
    await state.update_data(bind_account_id=str(account.id))
    await call.answer()
    await edit(call, "Введите слово или фразу, которая должна встречаться в названии лота.", back_menu("tgacc:open"))


@router.message(TelegramAccountsState.rule_keyword, F.text, ~F.text.startswith("/"))
async def tgacc_keyword_received(message: Message, state: FSMContext) -> None:
    if not valid_keyword(message.text):
        await message.answer("Введите слово или фразу длиной до 128 символов.")
        return
    await state.set_state(None)
    await state.update_data(bind_keyword=message.text.strip())
    await message.answer("Выберите страну для этой привязки.", reply_markup=bind_country_buttons())


@router.callback_query(F.data.startswith("tgacc:bind:"))
async def tgacc_bind_lot_country(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    parts = call.data.split(":", 2)
    choice = parts[2] if len(parts) > 2 else ""

    data = await state.get_data()
    lot_id = data.get("bind_lot_id")
    keyword = data.get("bind_keyword")

    if (not lot_id and not keyword) or data.get("bind_account_id") != str(account.id):
        await call.answer("Откройте привязку заново для выбранного аккаунта.", show_alert=True)
        return

    if choice == "custom":
        await state.set_state(TelegramAccountsState.rule_country)
        await call.answer()
        await edit(
            call,
            "🌐 Введите название страны текстом:",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:lots")]]),
        )
        return

    try:
        idx = int(choice)
        country = COUNTRY_PRESETS[idx][0]
    except (ValueError, IndexError):
        await call.answer("Выберите страну из списка.", show_alert=True)
        return
    if keyword:
        await bind_keyword(svc().db, account, keyword, country)
        await state.clear()
        await tgacc_rules_list(call)
        return

    lot_title = data.get("bind_lot_title")
    if not lot_title:
        try:
            client = await svc().gateway.get_client(account)
            item = await client.get_item(lot_id)
            lot_title = getattr(item, "name", f"Лот #{lot_id}")
        except Exception:
            lot_title = f"Лот #{lot_id}"

    async with svc().db() as session:
        existing = await session.scalar(
            select(TelegramAccountLotRule).where(
                TelegramAccountLotRule.account_id == account.id,
                TelegramAccountLotRule.lot_id == lot_id,
            )
        )
        if existing:
            existing.country = country
            existing.lot_title = lot_title
            existing.enabled = True
        else:
            rule = TelegramAccountLotRule(
                account_id=account.id,
                lot_id=lot_id,
                lot_title=lot_title,
                country=country,
                enabled=True,
            )
            session.add(rule)
        await session.commit()

    await call.answer("Лот успешно привязан!")
    await edit(
        call,
        f"✅ <b>Лот успешно привязан!</b>\n\n"
        f"📦 Товар: <b>{html.escape(lot_title)}</b>\n"
        f"🌍 Страна выдачи: <b>{html.escape(country)}</b>\n\n"
        "Теперь при оплате этого товара покупатель сразу получит номер данной страны и команду <code>#код</code>.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Привязанные лоты", callback_data="tgacc:rules")],
            [InlineKeyboardButton(text="📱 Меню плагина", callback_data="tgacc:open")],
        ]),
    )


@router.message(TelegramAccountsState.rule_country)
async def tgacc_rule_custom_country(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        return
    data = await state.get_data()
    lot_id = data.get("bind_lot_id")
    keyword = data.get("bind_keyword")
    country = (message.text or "").strip()
    if (not lot_id and not keyword) or data.get("bind_account_id") != str(account.id):
        await message.answer("Некорректный ввод.")
        await state.clear()
        return
    if not 1 <= len(country) <= 128:
        await message.answer("Введите название страны длиной до 128 символов.")
        return
    if keyword:
        await bind_keyword(svc().db, account, keyword, country)
        await state.clear()
        await message.answer("Привязка по фразе сохранена.", reply_markup=back_menu("tgacc:rules"))
        return

    try:
        client = await svc().gateway.get_client(account)
        item = await client.get_item(lot_id)
        lot_title = getattr(item, "name", f"Лот #{lot_id}")
    except Exception:
        lot_title = f"Лот #{lot_id}"

    async with svc().db() as session:
        existing = await session.scalar(
            select(TelegramAccountLotRule).where(
                TelegramAccountLotRule.account_id == account.id,
                TelegramAccountLotRule.lot_id == lot_id,
            )
        )
        if existing:
            existing.country = country
            existing.lot_title = lot_title
            existing.enabled = True
        else:
            rule = TelegramAccountLotRule(
                account_id=account.id,
                lot_id=lot_id,
                lot_title=lot_title,
                country=country,
                enabled=True,
            )
            session.add(rule)
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>Лот успешно привязан!</b>\n\n"
        f"📦 Товар: <b>{html.escape(lot_title)}</b>\n"
        f"🌍 Страна выдачи: <b>{html.escape(country)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Привязанные лоты", callback_data="tgacc:rules")],
            [InlineKeyboardButton(text="📱 Меню плагина", callback_data="tgacc:open")],
        ]),
        parse_mode="HTML",
    )


# --- Список привязанных лотов ---

@router.callback_query(F.data == "tgacc:rules")
async def tgacc_rules_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        rules = list((await session.scalars(
            select(TelegramAccountLotRule)
            .where(TelegramAccountLotRule.account_id == account.id)
            .order_by(TelegramAccountLotRule.id.desc())
        )).all())

    if not rules:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Привязать лот", callback_data="tgacc:lots")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="tgacc:open")],
        ])
        await call.answer()
        await edit(call, "📋 У вас пока нет привязанных лотов к Telegram.", markup)
        return

    b = InlineKeyboardBuilder()
    for r in rules:
        status_icon = "🟢" if r.enabled else "⏸"
        label = f"Фраза «{r.keyword}»" if r.keyword else r.lot_title
        b.button(text=f"{status_icon} {clip(label, 25)} ➔ {r.country}", callback_data=f"tgacc:rule:view:{r.id}")
        b.button(text="🗑", callback_data=f"tgacc:rule:del:{r.id}")
    b.adjust(2)
    b.row(
        InlineKeyboardButton(text="➕ Привязать ещё лот", callback_data="tgacc:lots"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="tgacc:open"),
    )

    await call.answer()
    await edit(call, f"📋 <b>Привязанные лоты ({len(rules)} шт.)</b>:\nНажмите на лот для настройки или 🗑 для удаления.", b.as_markup())


@router.callback_query(F.data.startswith("tgacc:rule:del:"))
async def tgacc_rule_delete(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    rule_id = int(call.data.split(":")[-1])
    async with svc().db() as session:
        r = await session.get(TelegramAccountLotRule, rule_id)
        if r and r.account_id == account.id:
            await session.delete(r)
            await session.commit()
    await call.answer("Привязка удалена")
    await tgacc_rules_list(call)


@router.callback_query(F.data.startswith("tgacc:rule:view:"))
async def tgacc_rule_view(call: CallbackQuery, rule_id: int | None = None) -> None:
    account = await require_account(call)
    if not account:
        return
    if rule_id is None:
        rule_id = int(call.data.split(":")[-1])
    async with svc().db() as session:
        r = await session.get(TelegramAccountLotRule, rule_id)
        if not r or r.account_id != account.id:
            await call.answer("Не найден", show_alert=True)
            return

    st_btn = "⏸ Приостановить" if r.enabled else "▶️ Включить"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=st_btn, callback_data=f"tgacc:rule:toggle:{r.id}")],
        [InlineKeyboardButton(text="🗑 Удалить привязку", callback_data=f"tgacc:rule:del:{r.id}")],
        [InlineKeyboardButton(text="⬅️ К списку лотов", callback_data="tgacc:rules")],
    ])

    await call.answer()
    await edit(
        call,
        (f"🔤 <b>Фраза:</b> {html.escape(r.keyword)}\n" if r.keyword else
         f"📦 <b>Лот:</b> {html.escape(r.lot_title)}\n🆔 ID лота: <code>{html.escape(r.lot_id)}</code>\n")
        +
        f"🌍 Страна выдачи: <b>{html.escape(r.country)}</b>\n"
        f"Статус: <b>{'🟢 Активен' if r.enabled else '⏸ Выключен'}</b>",
        markup,
    )


@router.callback_query(F.data.startswith("tgacc:rule:toggle:"))
async def tgacc_rule_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    rule_id = int(call.data.split(":")[-1])
    async with svc().db() as session:
        r = await session.get(TelegramAccountLotRule, rule_id)
        if r and r.account_id == account.id:
            r.enabled = not r.enabled
            await session.commit()
    await call.answer("Сохранено")
    await tgacc_rule_view(call, rule_id=rule_id)


# --- Настройки Telethon API ---

@router.callback_query(F.data == "tgacc:settings")
async def tgacc_settings_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    cfg = await get_or_create_config(account.id)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Изменить API ID", callback_data="tgacc:set:api_id")],
        [InlineKeyboardButton(text="🔒 Изменить API Hash", callback_data="tgacc:set:api_hash")],
        [InlineKeyboardButton(text="🔄 Сбросить к стандартным", callback_data="tgacc:set:reset")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="tgacc:open")],
    ])

    await call.answer()
    await edit(
        call,
        "⚙️ <b>Настройки Telethon MTProto</b>\n\n"
        f"API ID: <code>{cfg.api_id}</code>\n"
        f"API Hash: {'задан' if cfg.api_hash or default_telegram_api_hash() else 'не задан'}\n\n"
        "<i>Данные Telegram API можно указать здесь или в окружении сервера.</i>",
        markup,
    )


@router.callback_query(F.data == "tgacc:set:reset")
async def tgacc_settings_reset(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        cfg = await session.scalar(select(TelegramAccountsConfig).where(TelegramAccountsConfig.account_id == account.id))
        if cfg:
            cfg.api_id = default_telegram_api_id()
            cfg.api_hash = default_telegram_api_hash()
            await session.commit()
    await call.answer("Сброшено к стандартным значениям")
    await tgacc_settings_view(call)


@router.callback_query(F.data == "tgacc:set:api_id")
async def tgacc_set_api_id(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TelegramAccountsState.api_id)
    await call.answer()
    await edit(
        call,
        "Введите новый Telegram API ID (целое число):",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:settings")]]),
    )


@router.message(TelegramAccountsState.api_id)
async def tgacc_api_id_entered(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("API ID должен состоять только из цифр:")
        return

    async with svc().db() as session:
        cfg = await get_or_create_config(account.id)
        cfg.api_id = int(text)
        session.add(cfg)
        await session.commit()

    await state.clear()
    await message.answer("✅ API ID успешно сохранён!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ К настройкам", callback_data="tgacc:settings")]]))


@router.callback_query(F.data == "tgacc:set:api_hash")
async def tgacc_set_api_hash(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TelegramAccountsState.api_hash)
    await call.answer()
    await edit(
        call,
        "Введите новый Telegram API Hash (шестнадцатеричная строка из 32 символов):",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:settings")]]),
    )


@router.message(TelegramAccountsState.api_hash)
async def tgacc_api_hash_entered(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        return
    text = (message.text or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", text):
        await message.answer("API Hash должен содержать 32 шестнадцатеричных символа.")
        return

    async with svc().db() as session:
        cfg = await get_or_create_config(account.id)
        cfg.api_hash = text
        session.add(cfg)
        await session.commit()

    await state.clear()
    await message.answer("✅ API Hash успешно сохранён!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ К настройкам", callback_data="tgacc:settings")]]))
