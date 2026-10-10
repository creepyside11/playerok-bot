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
from .models import PlayerokAccount
from .states import GmailSellerState
from .gmail_accounts_manager import (
    GmailDeal,
    GmailLotRule,
    GmailStock,
    clean_totp_secret,
    format_delivery_message,
    generate_totp_code,
)

logger = logging.getLogger("gmail_accounts_ui")
router = Router(name="gmail_accounts_ui")


# --- Главное меню Gmail Seller ---

@router.callback_query(F.data == "gmail:open")
async def gmail_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        total_stocks = await session.scalar(
            select(func.count(GmailStock.id)).where(GmailStock.account_id == account.id)
        ) or 0
        avail_stocks = await session.scalar(
            select(func.count(GmailStock.id)).where(
                GmailStock.account_id == account.id,
                GmailStock.status == "available",
            )
        ) or 0
        issued_stocks = await session.scalar(
            select(func.count(GmailStock.id)).where(
                GmailStock.account_id == account.id,
                GmailStock.status == "issued",
            )
        ) or 0
        total_rules = await session.scalar(
            select(func.count(GmailLotRule.id)).where(GmailLotRule.account_id == account.id)
        ) or 0

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт (пошагово)", callback_data="gmail:add:start")],
        [InlineKeyboardButton(text="📥 Массовая загрузка (email:pass:2fa)", callback_data="gmail:add:bulk")],
        [InlineKeyboardButton(text="🔑 Генератор 2FA (для вас)", callback_data="gmail:totp:menu")],
        [InlineKeyboardButton(text=f"📦 Склад аккаунтов ({avail_stocks} шт.)", callback_data="gmail:stock:list:0")],
        [InlineKeyboardButton(text=f"🔗 Привязать лот Playerok ({total_rules})", callback_data="gmail:lots")],
        [InlineKeyboardButton(text="📋 Список привязанных лотов", callback_data="gmail:rules")],
        [InlineKeyboardButton(text="⬅️ Меню плагинов", callback_data="plugins:mine")],
    ])

    text = (
        "📧 <b>Gmail Автовыдача с 2FA (TOTP)</b>\n\n"
        "Плагин автоматически выдаёт покупателю Email, Пароль и 2FA-ключ, "
        "а также отправляет свежие 6-значные коды по команде <code>#2fa</code> в чате!\n\n"
        f"📊 <b>Статистика склада:</b>\n"
        f"• В наличии: <b>{avail_stocks}</b> шт.\n"
        f"• Уже выдано: <b>{issued_stocks}</b> шт.\n"
        f"• Всего в базе: <b>{total_stocks}</b> шт.\n"
        f"• Привязано лотов: <b>{total_rules}</b>"
    )
    await edit(call, text, markup)


# --- Пошаговое добавление аккаунта ---

@router.callback_query(F.data == "gmail:add:start")
async def gmail_add_step1(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return

    await state.set_state(GmailSellerState.add_email)
    text = (
        "➕ <b>Добавление аккаунта Gmail (Шаг 1 из 3)</b>\n\n"
        "Отправьте адрес электронной почты (Email):\n"
        "<i>Пример: myemail@gmail.com</i>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="gmail:open")]
    ])
    await edit(call, text, markup)


@router.message(GmailSellerState.add_email)
async def gmail_add_step1_msg(message: Message, state: FSMContext) -> None:
    email_text = (message.text or "").strip()
    if "@" not in email_text or "." not in email_text:
        await message.answer(
            "⚠️ Некорректный адрес почты. Пожалуйста, отправьте валидный email:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="gmail:open")]
            ])
        )
        return

    await state.update_data(email=email_text)
    await state.set_state(GmailSellerState.add_password)

    await message.answer(
        f"✅ Email: <code>{html.escape(email_text)}</code>\n\n"
        "➕ <b>Шаг 2 из 3: Введите пароль от аккаунта:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="gmail:open")]
        ])
    )


@router.message(GmailSellerState.add_password)
async def gmail_add_step2_msg(message: Message, state: FSMContext) -> None:
    password = (message.text or "").strip()
    if not password:
        await message.answer("⚠️ Пароль не может быть пустым. Введите пароль:")
        return

    await state.update_data(password=password)
    await state.set_state(GmailSellerState.add_totp)

    await message.answer(
        "➕ <b>Шаг 3 из 3: Введите секретный ключ 2FA (TOTP):</b>\n\n"
        "<i>Это секретный ключ из букв и цифр, который выдаёт Google при включении "
        "двухэтапной аутентификации (например: JBSWY3DPEHPK3PXP).\n"
        "Бот сразу проверит его и рассчитает тестовый код!</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="gmail:open")]
        ])
    )


@router.message(GmailSellerState.add_totp)
async def gmail_add_step3_msg(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id if message.from_user else 0)
    if not account:
        await message.answer("❌ Аккаунт Playerok не выбран.")
        await state.clear()
        return

    raw_totp = (message.text or "").strip()
    totp_clean = clean_totp_secret(raw_totp)
    code, rem = generate_totp_code(totp_clean)

    if not code:
        await message.answer(
            "❌ <b>Невалидный TOTP-ключ!</b>\n"
            "Не удалось сгенерировать проверочный 6-значный код. "
            "Проверьте ключ и попробуйте снова (или нажмите Отмена):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="gmail:open")]
            ])
        )
        return

    data = await state.get_data()
    email_text = data.get("email")
    password = data.get("password")
    await state.clear()

    cipher = svc().cipher
    pwd_enc = cipher.encrypt(password)
    totp_enc = cipher.encrypt(totp_clean)

    async with svc().db() as session:
        stock = GmailStock(
            account_id=account.id,
            email=email_text,
            password_encrypted=pwd_enc,
            totp_secret_encrypted=totp_enc,
            status="available",
        )
        session.add(stock)
        await session.commit()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="gmail:add:start")],
        [InlineKeyboardButton(text="📦 К складу", callback_data="gmail:stock:list:0")],
        [InlineKeyboardButton(text="📧 Меню плагина", callback_data="gmail:open")],
    ])

    await message.answer(
        "🎉 <b>Аккаунт успешно добавлен в базу!</b>\n\n"
        f"📧 Email: <code>{html.escape(email_text)}</code>\n"
        f"🔑 2FA ключ: <code>{html.escape(totp_clean)}</code>\n"
        f"✅ Проверочный 2FA-код прямо сейчас: <code>{code}</code> (ещё {rem} сек.)\n\n"
        "<i>Не забудьте выйти со своего устройства из этого Google-аккаунта перед продажей!</i>",
        reply_markup=markup,
    )


# --- Массовое добавление аккаунтов ---

@router.callback_query(F.data == "gmail:add:bulk")
async def gmail_bulk_add_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return

    await state.set_state(GmailSellerState.bulk_add)
    text = (
        "📥 <b>Массовая загрузка аккаунтов Gmail</b>\n\n"
        "Отправьте список аккаунтов сообщением. Каждый аккаунт с новой строки в одном из форматов:\n"
        "<code>email:password:totp_secret</code>\n"
        "или\n"
        "<code>email:password:totp_secret:recovery_email</code>\n\n"
        "<i>Пример:</i>\n"
        "<code>ivan123@gmail.com:SecretPass1:JBSWY3DPEHPK3PXP</code>\n"
        "<code>alex999@gmail.com:MyPassword2:MZXW6YTBOI======</code>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="gmail:open")]
    ])
    await edit(call, text, markup)


@router.message(GmailSellerState.bulk_add)
async def gmail_bulk_add_process(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id if message.from_user else 0)
    if not account:
        await message.answer("❌ Аккаунт Playerok не выбран.")
        await state.clear()
        return

    text = (message.text or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        await message.answer("⚠️ Текст пуст. Отправьте строки с аккаунтами:")
        return

    added = 0
    errors: list[str] = []
    cipher = svc().cipher

    async with svc().db() as session:
        for idx, line in enumerate(lines, start=1):
            parts = line.split(":")
            if len(parts) < 3:
                # Попробуем разделить по точке с запятой или табуляции
                if ";" in line:
                    parts = line.split(";")
                elif "\t" in line:
                    parts = line.split("\t")

            if len(parts) < 3:
                errors.append(f"Строка {idx}: неверный формат (нужно минимум 3 поля)")
                continue

            mail = parts[0].strip()
            pwd = parts[1].strip()
            totp = clean_totp_secret(parts[2].strip())
            recovery = parts[3].strip() if len(parts) > 3 else None

            code, _ = generate_totp_code(totp)
            if not code:
                errors.append(f"Строка {idx} ({mail}): невалидный TOTP-секрет")
                continue

            stock = GmailStock(
                account_id=account.id,
                email=mail,
                password_encrypted=cipher.encrypt(pwd),
                totp_secret_encrypted=cipher.encrypt(totp),
                recovery_email=recovery,
                status="available",
            )
            session.add(stock)
            added += 1

        await session.commit()

    await state.clear()
    res_text = f"✅ <b>Успешно добавлено: {added} акк.</b>\n"
    if errors:
        res_text += f"\n⚠️ Ошибок ({len(errors)}):\n" + "\n".join(errors[:10])
        if len(errors) > 10:
            res_text += f"\n<i>...и ещё {len(errors) - 10} ошибок</i>"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 К складу", callback_data="gmail:stock:list:0")],
        [InlineKeyboardButton(text="📧 Меню плагина", callback_data="gmail:open")],
    ])
    await message.answer(res_text, reply_markup=markup)


# --- 🔑 Генератор 2FA для продавца ---

@router.callback_query(F.data == "gmail:totp:menu")
async def gmail_totp_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Ввести TOTP-ключ вручную", callback_data="gmail:totp:manual")],
        [InlineKeyboardButton(text="📋 Выбрать аккаунт из базы", callback_data="gmail:totp:from_stock:0")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="gmail:open")],
    ])

    text = (
        "🔑 <b>Генератор 2FA (Google Authenticator)</b>\n\n"
        "Вам не нужно устанавливать Google Authenticator на телефон или сторонние программы.\n"
        "Вы можете получить свежий 6-значный код прямо здесь:"
    )
    await edit(call, text, markup)


@router.callback_query(F.data == "gmail:totp:manual")
async def gmail_totp_manual_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GmailSellerState.generate_totp)
    text = (
        "🔑 <b>Ввод TOTP-ключа</b>\n\n"
        "Отправьте секретный ключ (например, <code>JBSWY3DPEHPK3PXP</code>):"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="gmail:totp:menu")]
    ])
    await edit(call, text, markup)


@router.message(GmailSellerState.generate_totp)
async def gmail_totp_manual_process(message: Message, state: FSMContext) -> None:
    raw_totp = (message.text or "").strip()
    totp_clean = clean_totp_secret(raw_totp)
    code, rem = generate_totp_code(totp_clean)

    if not code:
        await message.answer(
            "❌ Невалидный секретный ключ. Попробуйте еще раз:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="gmail:totp:menu")]
            ])
        )
        return

    await state.clear()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить код", callback_data=f"gmail:totp:refresh:{totp_clean}")],
        [InlineKeyboardButton(text="🔑 Другой ключ", callback_data="gmail:totp:manual")],
        [InlineKeyboardButton(text="⬅️ В меню плагина", callback_data="gmail:open")],
    ])

    await message.answer(
        f"🔑 <b>Код подтверждения (2FA):</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"⏳ Действителен ещё: <b>{rem} сек.</b>\n\n"
        f"Ключ: <code>{totp_clean}</code>",
        reply_markup=markup,
    )


@router.callback_query(F.data.startswith("gmail:totp:refresh:"))
async def gmail_totp_refresh(call: CallbackQuery) -> None:
    secret = call.data.split(":", 3)[-1]
    code, rem = generate_totp_code(secret)
    if not code:
        await call.answer("Ошибка ключа", show_alert=True)
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить код", callback_data=f"gmail:totp:refresh:{secret}")],
        [InlineKeyboardButton(text="🔑 Другой ключ", callback_data="gmail:totp:manual")],
        [InlineKeyboardButton(text="⬅️ В меню плагина", callback_data="gmail:open")],
    ])

    text = (
        f"🔑 <b>Код подтверждения (2FA):</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"⏳ Действителен ещё: <b>{rem} сек.</b>\n\n"
        f"Ключ: <code>{secret}</code>"
    )
    await edit(call, text, markup)
    await call.answer("Код обновлён")


@router.callback_query(F.data.startswith("gmail:totp:from_stock:"))
async def gmail_totp_from_stock(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    page = int(call.data.rsplit(":", 1)[-1])
    per_page = 8

    async with svc().db() as session:
        stocks = (await session.scalars(
            select(GmailStock)
            .where(GmailStock.account_id == account.id)
            .order_by(GmailStock.id.desc())
            .offset(page * per_page)
            .limit(per_page + 1)
        )).all()

    has_next = len(stocks) > per_page
    stocks = stocks[:per_page]

    if not stocks:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="gmail:add:start")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="gmail:totp:menu")],
        ])
        await edit(call, "📦 В базе ещё нет добавленных аккаунтов.", markup)
        return

    b = InlineKeyboardBuilder()
    for s in stocks:
        st_icon = "🟢" if s.status == "available" else "🔴"
        b.button(text=f"{st_icon} {clip(s.email, 22)}", callback_data=f"gmail:totp:show_stock:{s.id}")
    b.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"gmail:totp:from_stock:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"gmail:totp:from_stock:{page + 1}"))
    if nav:
        b.row(*nav)

    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="gmail:totp:menu"))

    await edit(call, "🔑 <b>Выберите аккаунт для получения 2FA-кода:</b>", b.as_markup())


@router.callback_query(F.data.startswith("gmail:totp:show_stock:"))
async def gmail_totp_show_stock(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    stock_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        stock = await session.get(GmailStock, stock_id)
        if not stock or stock.account_id != account.id:
            await call.answer("Аккаунт не найден", show_alert=True)
            return

    cipher = svc().cipher
    secret = cipher.decrypt(stock.totp_secret_encrypted)
    code, rem = generate_totp_code(secret)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить код", callback_data=f"gmail:totp:refresh:{secret}")],
        [InlineKeyboardButton(text="📋 К списку аккаунтов", callback_data="gmail:totp:from_stock:0")],
        [InlineKeyboardButton(text="⬅️ В меню плагина", callback_data="gmail:open")],
    ])

    text = (
        f"📧 <b>Аккаунт:</b> <code>{html.escape(stock.email)}</code>\n\n"
        f"🔑 <b>Код 2FA:</b> <code>{code}</code>\n"
        f"⏳ Действителен ещё: <b>{rem} сек.</b>\n\n"
        f"Секрет: <code>{secret}</code>"
    )
    await edit(call, text, markup)


# --- Склад аккаунтов (Список, просмотр, удаление) ---

@router.callback_query(F.data.startswith("gmail:stock:list:"))
async def gmail_stock_list(call: CallbackQuery, page: int | None = None) -> None:
    account = await require_account(call)
    if not account:
        return

    if page is None:
        page = int(call.data.rsplit(":", 1)[-1])
    per_page = 8

    async with svc().db() as session:
        stocks = (await session.scalars(
            select(GmailStock)
            .where(GmailStock.account_id == account.id)
            .order_by(GmailStock.id.desc())
            .offset(page * per_page)
            .limit(per_page + 1)
        )).all()

    has_next = len(stocks) > per_page
    stocks = stocks[:per_page]

    if not stocks:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="gmail:add:start")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="gmail:open")],
        ])
        await edit(call, "📦 Склад пуст. Добавьте первый аккаунт для автовыдачи!", markup)
        return

    b = InlineKeyboardBuilder()
    for s in stocks:
        st_icon = "🟢" if s.status == "available" else "🔴"
        b.button(text=f"{st_icon} {clip(s.email, 24)}", callback_data=f"gmail:stock:view:{s.id}")
        b.button(text="🗑", callback_data=f"gmail:stock:del:{s.id}:{page}")
    b.adjust(2)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"gmail:stock:list:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"gmail:stock:list:{page + 1}"))
    if nav:
        b.row(*nav)

    b.row(InlineKeyboardButton(text="➕ Добавить", callback_data="gmail:add:start"))
    b.row(InlineKeyboardButton(text="⬅️ В меню плагина", callback_data="gmail:open"))

    text = (
        f"📦 <b>Склад аккаунтов Gmail (Стр. {page + 1})</b>\n\n"
        "🟢 — Готов к продаже\n"
        "🔴 — Выдан покупателю\n\n"
        "<i>Нажмите на аккаунт для просмотра данных или 🗑 для удаления.</i>"
    )
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("gmail:stock:view:"))
async def gmail_stock_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    stock_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        stock = await session.get(GmailStock, stock_id)
        if not stock or stock.account_id != account.id:
            await call.answer("Аккаунт не найден", show_alert=True)
            return

    cipher = svc().cipher
    password = cipher.decrypt(stock.password_encrypted)
    totp = cipher.decrypt(stock.totp_secret_encrypted)
    code, rem = generate_totp_code(totp)

    status_str = "🟢 В наличии (готов к продаже)" if stock.status == "available" else f"🔴 Выдан в сделке #{stock.issued_deal_id}"

    text = (
        f"📧 <b>Информация об аккаунте #{stock.id}</b>\n\n"
        f"Статус: <b>{status_str}</b>\n"
        f"Почта: <code>{html.escape(stock.email)}</code>\n"
        f"Пароль: <code>{html.escape(password)}</code>\n"
        f"2FA Секрет: <code>{html.escape(totp)}</code>\n\n"
        f"🔑 Текущий 2FA-код: <code>{code}</code> (ещё {rem} сек.)"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Получить/Обновить 2FA", callback_data=f"gmail:totp:show_stock:{stock.id}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"gmail:stock:del:{stock.id}:0")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="gmail:stock:list:0")],
    ])
    await edit(call, text, markup)


@router.callback_query(F.data.startswith("gmail:stock:del:"))
async def gmail_stock_delete(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    parts = call.data.split(":")
    stock_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 0

    async with svc().db() as session:
        await session.execute(
            delete(GmailStock).where(
                GmailStock.id == stock_id,
                GmailStock.account_id == account.id,
            )
        )
        await session.commit()

    await call.answer("Аккаунт удален")
    await gmail_stock_list(call, page=page)


# --- Привязка лотов Playerok к автовыдаче ---

@router.callback_query(F.data == "gmail:lots")
async def gmail_choose_lot(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    try:
        client = await svc().gateway.get_client(account)
        res = await client.call("get_my_items", statuses=None, count=24)
        items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        logger.warning("Ошибка получения лотов Playerok: %s", exc)
        await call.answer(f"Ошибка загрузки лотов: {exc}", show_alert=True)
        return

    if not items:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="gmail:open")]
        ])
        await edit(call, "📦 У вас нет активных лотов на Playerok.", markup)
        return

    b = InlineKeyboardBuilder()
    for item in items:
        b.button(text=f"📦 {clip(item.name, 26)}", callback_data=f"gmail:bind_lot:{item.id}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="gmail:open"))

    text = (
        "🔗 <b>Привязка лота к автовыдаче Gmail</b>\n\n"
        "Выберите лот на Playerok, при покупке которого бот автоматически выдаст аккаунт Gmail:"
    )
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("gmail:bind_lot:"))
async def gmail_bind_lot_confirm(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    lot_id = call.data.rsplit(":", 1)[-1]
    try:
        client = await svc().gateway.get_client(account)
        item = await client.get_item(lot_id)
        lot_title = getattr(item, "name", f"Лот {lot_id}")
    except Exception:
        lot_title = f"Лот {lot_id}"

    async with svc().db() as session:
        existing = await session.scalar(
            select(GmailLotRule).where(
                GmailLotRule.account_id == account.id,
                GmailLotRule.lot_id == lot_id,
            )
        )
        if existing:
            existing.lot_title = lot_title
            existing.enabled = True
        else:
            rule = GmailLotRule(
                account_id=account.id,
                lot_id=lot_id,
                lot_title=lot_title,
                enabled=True,
            )
            session.add(rule)

        # Автоматически активируем плагин gmail_seller в plugin_states
        from .plugin_system import PluginState
        p_state = await session.scalar(
            select(PluginState).where(
                PluginState.account_id == account.id,
                PluginState.plugin_id == "gmail_seller",
            )
        )
        if not p_state:
            session.add(PluginState(account_id=account.id, plugin_id="gmail_seller", enabled=True, config={}))
        else:
            p_state.enabled = True

        await session.commit()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список привязанных лотов", callback_data="gmail:rules")],
        [InlineKeyboardButton(text="🔗 Привязать ещё лот", callback_data="gmail:lots")],
        [InlineKeyboardButton(text="📧 Меню плагина", callback_data="gmail:open")],
    ])

    await edit(
        call,
        f"✅ <b>Лот успешно привязан!</b>\n\n"
        f"📦 Товар: <b>{html.escape(lot_title)}</b>\n"
        f"ID: <code>{lot_id}</code>\n\n"
        "Теперь при покупке этого лота бот автоматически выдаст аккаунт Gmail из склада "
        "и подтвердит выполнение сделки!",
        markup,
    )


# --- Список привязанных лотов ---

@router.callback_query(F.data == "gmail:rules")
async def gmail_rules_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        rules = (await session.scalars(
            select(GmailLotRule)
            .where(GmailLotRule.account_id == account.id)
            .order_by(GmailLotRule.id.desc())
        )).all()

    if not rules:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Привязать лот", callback_data="gmail:lots")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="gmail:open")],
        ])
        await edit(call, "📋 Пока нет привязанных лотов.", markup)
        return

    b = InlineKeyboardBuilder()
    for r in rules:
        status_icon = "🟢" if r.enabled else "⏸"
        b.button(text=f"{status_icon} {clip(r.lot_title, 24)}", callback_data=f"gmail:rule:toggle:{r.id}")
        b.button(text="🗑", callback_data=f"gmail:rule:del:{r.id}")
    b.adjust(2)

    b.row(InlineKeyboardButton(text="➕ Привязать ещё лот", callback_data="gmail:lots"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="gmail:open"))

    text = (
        "📋 <b>Привязанные лоты Gmail автовыдачи</b>\n\n"
        "🟢 — Активен (автовыдача включена)\n"
        "⏸ — На паузе (нажмите на название для переключения)\n"
        "🗑 — Удалить привязку"
    )
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("gmail:rule:toggle:"))
async def gmail_rule_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(GmailLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.enabled = not rule.enabled
            await session.commit()

    await call.answer("Статус обновлен")
    await gmail_rules_list(call)


@router.callback_query(F.data.startswith("gmail:rule:del:"))
async def gmail_rule_del(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        await session.execute(
            delete(GmailLotRule).where(
                GmailLotRule.id == rule_id,
                GmailLotRule.account_id == account.id,
            )
        )
        await session.commit()

    await call.answer("Привязка удалена")
    await gmail_rules_list(call)
