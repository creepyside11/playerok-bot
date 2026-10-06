from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import logging
import re
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select

from .handlers import active_account, clip, edit, require_account, svc
from .keyboards import back_menu
from .models import PlayerokAccount
from .states import TelegramAccountsState
from .telegram_accounts_manager import (
    COUNTRY_PRESETS,
    DEFAULT_TG_API_HASH,
    DEFAULT_TG_API_ID,
    TelegramAccountDeal,
    TelegramAccountLotRule,
    TelegramAccountStock,
    TelegramAccountsConfig,
)

logger = logging.getLogger("telegram_accounts_ui")
router = Router(name="telegram_accounts_ui")

# In-memory dictionary for pending interactive Telethon logins
# key: tg_user_id -> dict(client, phone, hash, country, account_id)
PENDING_PHONE_AUTHS: dict[int, dict[str, Any]] = {}


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
        [InlineKeyboardButton(text=f"📋 Привязанные лоты ({total_rules})", callback_data="tgacc:rules")],
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
        [InlineKeyboardButton(text="📝 Добавить готовую StringSession", callback_data="tgacc:add:raw_start")],
        [InlineKeyboardButton(text="📋 Список всех номеров", callback_data="tgacc:stock:list:0")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="tgacc:open")],
    ])

    text = (
        "👥 <b>Менеджер аккаунтов Telegram</b>\n\n"
        "<b>Наличие по странам:</b>\n"
        f"{breakdown_text}\n\n"
        "Для добавления нажмите кнопку ниже. Бот запросит код подтверждения через официальный MTProto/Telethon."
    )
    await call.answer()
    await edit(call, text, markup)


# --- Добавление: Выбор страны ---

@router.callback_query(F.data == "tgacc:add:country")
async def tgacc_add_choose_country(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

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
    await state.set_state(TelegramAccountsState.add_phone)
    await call.answer()
    await edit(
        call,
        f"🌍 Страна: <b>{html.escape(country)}</b>\n\n"
        "📱 Введите номер телефона в международном формате\n"
        "<i>Пример: <code>+79991234567</code> или <code>+12025550123</code>:</i>",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:manager")]]),
    )


@router.message(TelegramAccountsState.custom_country)
async def tgacc_custom_country_entered(message: Message, state: FSMContext) -> None:
    country = (message.text or "").strip()
    if not country:
        await message.answer("Введите корректное название страны:")
        return
    await state.update_data(chosen_country=country)
    await state.set_state(TelegramAccountsState.add_phone)
    await message.answer(
        f"🌍 Страна: <b>{html.escape(country)}</b>\n\n"
        "📱 Введите номер телефона в международном формате\n"
        "<i>Пример: <code>+79991234567</code>:</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:manager")]]),
        parse_mode="HTML",
    )


@router.message(TelegramAccountsState.add_phone)
async def tgacc_phone_entered(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await message.answer("Аккаунт Playerok не выбран.")
        await state.clear()
        return

    raw_phone = (message.text or "").strip()
    clean_phone = re.sub(r"[^\d+]", "", raw_phone)
    if not clean_phone.startswith("+"):
        clean_phone = "+" + clean_phone

    if not re.match(r"^\+\d{7,16}$", clean_phone):
        await message.answer("❌ Некорректный формат номера. Введите номер с кодом страны, например: <code>+79991234567</code>", parse_mode="HTML")
        return

    data = await state.get_data()
    country = data.get("chosen_country", "Другая")

    cfg = await get_or_create_config(account.id)
    api_id = cfg.api_id or DEFAULT_TG_API_ID
    api_hash = cfg.api_hash or DEFAULT_TG_API_HASH

    status_msg = await message.answer("⏳ Подключаюсь к Telegram и отправляю код запроса…")

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
        await cleanup_pending_auth(message.from_user.id)
        await state.clear()
        return

    # Сохраняем активный клиент в памяти
    PENDING_PHONE_AUTHS[message.from_user.id] = {
        "client": client,
        "phone": clean_phone,
        "phone_code_hash": res.phone_code_hash,
        "country": country,
        "account_id": account.id,
    }

    await state.set_state(TelegramAccountsState.add_code)
    await status_msg.edit_text(
        f"📨 <b>Код отправлен в Telegram!</b>\n\n"
        f"📱 Номер: <code>{html.escape(clean_phone)}</code>\n"
        f"🌍 Страна: <b>{html.escape(country)}</b>\n\n"
        "Введите 5-значный код подтверждения (например: <code>12345</code> или <code>1 2 3 4 5</code>):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tgacc:cancel_auth")]]),
        parse_mode="HTML",
    )


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
    clean_code = re.sub(r"\D", "", code_raw)
    if not clean_code:
        await message.answer("Введите цифровой код:")
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
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(session_str), cfg.api_id, cfg.api_hash)
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
async def tgacc_stock_list_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
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
    call.data = f"tgacc:stock:list:{page}"
    await tgacc_stock_list_view(call)


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
    await state.update_data(bind_lot_id=lot_id)

    # Получаем название лота
    try:
        client = await svc().gateway.get_client(account)
        item = await client.get_item(lot_id)
        lot_title = getattr(item, "name", f"Лот #{lot_id}")
    except Exception:
        lot_title = f"Лот #{lot_id}"

    await state.update_data(bind_lot_title=lot_title)

    b = InlineKeyboardBuilder()
    for idx, (name, _) in enumerate(COUNTRY_PRESETS):
        b.button(text=name, callback_data=f"tgacc:bind:{idx}")
    b.button(text="🌐 Другая страна", callback_data="tgacc:bind:custom")
    b.button(text="❌ Отмена", callback_data="tgacc:lots")
    b.adjust(2, 2, 2, 2, 1, 1)

    await call.answer()
    await edit(
        call,
        f"📦 Лот: <b>{html.escape(lot_title)}</b>\n\n"
        "🌍 <b>Выберите страну, профиль которой нужно выдавать при покупке этого лота:</b>",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("tgacc:bind:"))
async def tgacc_bind_lot_country(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    parts = call.data.split(":", 2)
    choice = parts[2] if len(parts) > 2 else ""

    data = await state.get_data()
    lot_id = data.get("bind_lot_id")

    if not lot_id:
        await call.answer("Сессия выбора лота устарела, выберите лот снова.", show_alert=True)
        await tgacc_lots_select(call)
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
        country = choice

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
    country = (message.text or "").strip()
    if not lot_id or not country:
        await message.answer("Некорректный ввод.")
        await state.clear()
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
        b.button(text=f"{status_icon} {clip(r.lot_title, 20)} ➔ {r.country}", callback_data=f"tgacc:rule:view:{r.id}")
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
async def tgacc_rule_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
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
        f"📦 <b>Лот:</b> {html.escape(r.lot_title)}\n"
        f"🆔 ID лота: <code>{html.escape(r.lot_id)}</code>\n"
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
    call.data = f"tgacc:rule:view:{rule_id}"
    await tgacc_rule_view(call)


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
        f"API Hash: <code>{cfg.api_hash[:6]}...{cfg.api_hash[-4:] if len(cfg.api_hash) > 10 else cfg.api_hash}</code>\n\n"
        "<i>По умолчанию используются публичные данные Telegram Desktop / Android, позволяющие авторизовывать номера без создания своего приложения на my.telegram.org.</i>",
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
            cfg.api_id = DEFAULT_TG_API_ID
            cfg.api_hash = DEFAULT_TG_API_HASH
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
    if len(text) < 16:
        await message.answer("Слишком короткий API Hash. Введите корректный хеш:")
        return

    async with svc().db() as session:
        cfg = await get_or_create_config(account.id)
        cfg.api_hash = text
        session.add(cfg)
        await session.commit()

    await state.clear()
    await message.answer("✅ API Hash успешно сохранён!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ К настройкам", callback_data="tgacc:settings")]]))
