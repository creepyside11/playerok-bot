from __future__ import annotations

import html
import io
import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select

from .handlers import active_account, clip, edit, main_menu, require_account, svc
from .plugin_system import PluginState
from .roblox_vc_manager import (
    DEFAULT_ROBLOX_DELIVERY_TEMPLATE,
    RobloxDeal,
    RobloxLotRule,
    RobloxStock,
    format_roblox_delivery_message,
    generate_roblox_totp,
    parse_roblox_bulk_lines,
)

logger = logging.getLogger("roblox_ui")
router = Router(name="roblox_ui")


class RobloxStates(StatesGroup):
    stock_add_lines = State()


# --- ГЛАВНОЕ МЕНЮ ROBLOX VOICE CHAT ---

@router.callback_query(F.data == "roblox_vc:open")
async def roblox_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        # Автоматически активируем плагин roblox_vc_seller
        p_state = await session.scalar(
            select(PluginState).where(
                PluginState.account_id == account.id,
                PluginState.plugin_id == "roblox_vc_seller",
            )
        )
        if not p_state:
            session.add(PluginState(account_id=account.id, plugin_id="roblox_vc_seller", enabled=True, config={}))
            await session.commit()
        elif not p_state.enabled:
            p_state.enabled = True
            await session.commit()

        # Считаем остаток на складе
        total_available = (await session.scalar(
            select(func.count(RobloxStock.id)).where(
                RobloxStock.account_id == account.id,
                RobloxStock.status == "available",
            )
        )) or 0
        total_with_2fa = (await session.scalar(
            select(func.count(RobloxStock.id)).where(
                RobloxStock.account_id == account.id,
                RobloxStock.status == "available",
                RobloxStock.totp_secret_encrypted.isnot(None),
            )
        )) or 0
        total_issued = (await session.scalar(
            select(func.count(RobloxStock.id)).where(
                RobloxStock.account_id == account.id,
                RobloxStock.status == "issued",
            )
        )) or 0
        rules_count = (await session.scalar(
            select(func.count(RobloxLotRule.id)).where(RobloxLotRule.account_id == account.id)
        )) or 0

        # Последние 5 доступных аккаунтов
        recent = (await session.scalars(
            select(RobloxStock).where(
                RobloxStock.account_id == account.id,
                RobloxStock.status == "available",
            ).order_by(RobloxStock.id.desc()).limit(5)
        )).all()

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="➕ Добавить аккаунты", callback_data="roblox_vc:add"),
        InlineKeyboardButton(text="📥 Экспорт базы .txt", callback_data="roblox_vc:export"),
    )
    builder.row(
        InlineKeyboardButton(text=f"🔗 Привязать лот Playerok ({rules_count})", callback_data="roblox_vc:rules:list"),
    )
    builder.row(
        InlineKeyboardButton(text="📜 История сделок", callback_data="roblox_vc:deals:list"),
        InlineKeyboardButton(text="🗑 Очистить выданные", callback_data="roblox_vc:clear_issued"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Меню плагинов", callback_data="plugins:mine"),
    )

    preview_lines = []
    for s in recent:
        has_2fa = "🔐 [2FA]" if s.totp_secret_encrypted else "🔓 [Без 2FA]"
        rbx = f" ({s.robux_balance:,} R$)" if s.robux_balance else ""
        preview_lines.append(f"• <code>{html.escape(s.username)}</code> {has_2fa}{rbx}")

    preview_text = "\n".join(preview_lines) if preview_lines else "<i>Склад пуст</i>"

    text = (
        "🎙️ <b>Roblox Voice Chat Автовыдача</b>\n\n"
        "Специализированная продажа аккаунтов Roblox с включенным голосовым чатом (13+ Verified).\n"
        "Поддерживает как аккаунты с 2FA (Authenticator), так и простые без 2FA.\n\n"
        f"📦 В наличии на складе: <b>{total_available} шт.</b> (из них с 2FA: <b>{total_with_2fa} шт.</b>)\n"
        f"✅ Успешно продано: <b>{total_issued} шт.</b>\n"
        f"🔗 Активных правил привязки: <b>{rules_count} шт.</b>\n\n"
        f"<b>Последние аккаунты на складе:</b>\n{preview_text}"
    )

    await edit(call, text, builder.as_markup())


# --- ДОБАВЛЕНИЕ АККАУНТОВ ROBLOX ---

@router.callback_query(F.data == "roblox_vc:add")
async def roblox_add_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return

    await state.set_state(RobloxStates.stock_add_lines)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="roblox_vc:open")],
    ])

    text = (
        "➕ <b>Добавление Roblox аккаунтов с Voice Chat</b>\n\n"
        "Отправьте данные аккаунтов сообщением (по одной строке на аккаунт):\n\n"
        "<b>Форматы строк:</b>\n"
        "1. Без 2FA:\n"
        "<code>username:password</code>\n\n"
        "2. С 2FA (TOTP ключом):\n"
        "<code>username:password:2FA_SECRET</code>\n\n"
        "3. С робуксами или комментарием:\n"
        "<code>username:password:2FA_SECRET:комментарий_или_робуксы</code>\n"
        "<i>(Если 2FA нет, а комментарий нужен — укажите <code>username:password::комментарий</code>)</i>\n\n"
        "<i>Разделителем может быть двоеточие <code>:</code>, точка с запятой <code>;</code> или знак табуляции.</i>"
    )

    await edit(call, text, markup)


@router.message(RobloxStates.stock_add_lines)
async def roblox_add_process(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await message.answer("Сначала выберите или добавьте аккаунт Playerok.")
        await state.clear()
        return

    text = (message.text or "").strip()
    parsed = parse_roblox_bulk_lines(text)
    if not parsed:
        await message.answer(
            "⚠️ Не удалось распознать аккаунты. Убедитесь, что формат <code>username:password</code> (хотя бы логин и пароль в строке)."
        )
        return

    cipher = svc().cipher
    added_count = 0
    with_2fa_count = 0

    async with svc().db() as session:
        for item in parsed:
            uname = item["username"]
            raw_pwd = item["password"]
            raw_totp = item.get("totp_secret")
            robux = item.get("robux_balance")
            comment = item.get("comment")

            pwd_enc = cipher.encrypt(raw_pwd) if cipher else raw_pwd
            totp_enc = cipher.encrypt(raw_totp) if (cipher and raw_totp) else raw_totp

            if raw_totp:
                with_2fa_count += 1

            stock = RobloxStock(
                account_id=account.id,
                username=uname,
                password_encrypted=pwd_enc,
                totp_secret_encrypted=totp_enc,
                robux_balance=robux,
                is_voice_chat_enabled=True,
                comment=comment,
                status="available",
            )
            session.add(stock)
            added_count += 1

        await session.commit()

    await state.clear()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎙️ В меню Roblox VC", callback_data="roblox_vc:open")],
    ])

    await message.answer(
        f"✅ <b>Успешно добавлено {added_count} аккаунтов Roblox VC</b>!\n"
        f"• С привязанным 2FA ключом: <b>{with_2fa_count} шт.</b>\n"
        f"• Без 2FA (прямой вход): <b>{added_count - with_2fa_count} шт.</b>",
        reply_markup=markup,
        parse_mode="HTML",
    )


# --- ЭКСПОРТ БАЗЫ ---

@router.callback_query(F.data == "roblox_vc:export")
async def roblox_export_file(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    cipher = svc().cipher

    async with svc().db() as session:
        stocks = (await session.scalars(
            select(RobloxStock).where(
                RobloxStock.account_id == account.id,
                RobloxStock.status == "available",
            ).order_by(RobloxStock.id.asc())
        )).all()

    if not stocks:
        await call.answer("На складе нет доступных аккаунтов для выгрузки.", show_alert=True)
        return

    lines = []
    for s in stocks:
        pwd = cipher.decrypt(s.password_encrypted) if cipher else s.password_encrypted
        totp = ""
        if s.totp_secret_encrypted:
            totp = cipher.decrypt(s.totp_secret_encrypted) if cipher else s.totp_secret_encrypted

        extra = f":{totp}" if totp else ""
        comm = f":{s.comment}" if s.comment else ""
        lines.append(f"{s.username}:{pwd}{extra}{comm}")

    file_bytes = "\n".join(lines).encode("utf-8")
    from aiogram.types import BufferedInputFile
    input_file = BufferedInputFile(file_bytes, filename=f"roblox_vc_{len(stocks)}_accs.txt")

    await call.message.answer_document(
        input_file,
        caption=f"📥 Выгрузка склада Roblox Voice Chat ({len(stocks)} шт.)",
        parse_mode="HTML",
    )
    await call.answer()


# --- ПРИВЯЗКА ЛОТОВ PLAYEROK ---

@router.callback_query(F.data == "roblox_vc:rules:list")
async def roblox_rules_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        rules = (await session.scalars(
            select(RobloxLotRule).where(
                RobloxLotRule.account_id == account.id
            ).order_by(RobloxLotRule.id.desc())
        )).all()

    builder = InlineKeyboardBuilder()
    for r in rules:
        status_icon = "🟢" if r.enabled else "🔴"
        builder.button(
            text=f"{status_icon} {clip(r.lot_title, 28)}",
            callback_data=f"roblox_rule_view:{r.id}",
        )
    builder.adjust(1)

    builder.row(
        InlineKeyboardButton(text="➕ Привязать лот Playerok", callback_data="roblox_rule_add:pick"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад в меню Roblox", callback_data="roblox_vc:open"),
    )

    text = (
        "🔗 <b>Привязанные лоты Playerok к выдаче Roblox VC:</b>\n\n"
        f"Всего правил: <b>{len(rules)} шт.</b>\n\n"
        "Когда покупатель оплачивает привязанный товар, бот выдает аккаунт с включенным Voice Chat, "
        "инструкцию и при наличии 2FA — выдает код прямо в чате."
    )

    await edit(call, text, builder.as_markup())


@router.callback_query(F.data == "roblox_rule_add:pick")
async def roblox_rule_add_pick(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    client = await svc().gateway.client_for(account)
    items = []
    if client:
        try:
            items = await client.get_items()
        except Exception as exc:
            logger.warning("Could not fetch playerok items for roblox rule: %s", exc)

    builder = InlineKeyboardBuilder()

    # Универсальное правило для всех лотов
    builder.button(
        text="🌐 Все лоты (*) -> Roblox VC",
        callback_data="roblox_rule_create:all_lots:Все лоты",
    )

    for it in (items or [])[:20]:
        it_id = str(getattr(it, "id", "") or "")
        it_name = str(getattr(it, "name", "") or "Товар")
        if it_id:
            builder.button(
                text=f"📦 {clip(it_name, 30)}",
                callback_data=f"roblox_rule_create:{it_id}:{it_name[:25]}",
            )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="roblox_vc:rules:list"))

    await edit(
        call,
        "📦 <b>Выберите лот Playerok для привязки к автовыдаче Roblox VC:</b>",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("roblox_rule_create:"))
async def roblox_rule_create_save(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    parts = call.data.split(":", 2)
    lot_id = parts[1]
    lot_title = parts[2] if len(parts) > 2 else "Товар"

    if lot_id == "all_lots":
        lot_id = "*"

    async with svc().db() as session:
        existing = await session.scalar(
            select(RobloxLotRule).where(
                RobloxLotRule.account_id == account.id,
                RobloxLotRule.lot_id == lot_id,
            )
        )
        if existing:
            existing.lot_title = lot_title
            existing.enabled = True
        else:
            rule = RobloxLotRule(
                account_id=account.id,
                lot_id=lot_id,
                lot_title=lot_title,
                delivery_template=DEFAULT_ROBLOX_DELIVERY_TEMPLATE,
                auto_sent=True,
                enabled=True,
            )
            session.add(rule)
        await session.commit()

    await call.answer("Лот успешно привязан!", show_alert=True)
    await roblox_rules_list(call)


@router.callback_query(F.data.startswith("roblox_rule_view:"))
async def roblox_rule_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.split(":", 1)[1])
    async with svc().db() as session:
        rule = await session.get(RobloxLotRule, rule_id)
        if not rule or rule.account_id != account.id:
            await call.answer("Правило не найдено", show_alert=True)
            return

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔴 Выключить" if rule.enabled else "🟢 Включить",
            callback_data=f"roblox_rule_toggle:{rule.id}",
        ),
        InlineKeyboardButton(
            text="🤖 Авто-SENT: ВКЛ" if rule.auto_sent else "🤖 Авто-SENT: ВЫКЛ",
            callback_data=f"roblox_rule_autosent:{rule.id}",
        ),
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Удалить правило", callback_data=f"roblox_rule_del:{rule.id}"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ К списку лотов", callback_data="roblox_vc:rules:list"),
    )

    text = (
        f"⚙️ <b>Настройка привязки лота Roblox:</b>\n\n"
        f"📦 Лот: <b>{html.escape(rule.lot_title)}</b> (ID: <code>{rule.lot_id}</code>)\n"
        f"Статус: <b>{'🟢 Активно' if rule.enabled else '🔴 Отключено'}</b>\n"
        f"Авто-завершение заказа: <b>{'Да (SENT)' if rule.auto_sent else 'Нет'}</b>\n\n"
        f"<b>Текущий шаблон выдачи:</b>\n"
        f"<code>{html.escape(rule.delivery_template[:300])}...</code>"
    )

    await edit(call, text, builder.as_markup())


@router.callback_query(F.data.startswith("roblox_rule_toggle:"))
async def roblox_rule_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.split(":", 1)[1])
    async with svc().db() as session:
        rule = await session.get(RobloxLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.enabled = not rule.enabled
            await session.commit()

    await call.answer("Статус изменен")
    await roblox_rule_view(call)


@router.callback_query(F.data.startswith("roblox_rule_autosent:"))
async def roblox_rule_autosent(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.split(":", 1)[1])
    async with svc().db() as session:
        rule = await session.get(RobloxLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.auto_sent = not rule.auto_sent
            await session.commit()

    await call.answer("Режим автозавершения изменен")
    await roblox_rule_view(call)


@router.callback_query(F.data.startswith("roblox_rule_del:"))
async def roblox_rule_del(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.split(":", 1)[1])
    async with svc().db() as session:
        rule = await session.get(RobloxLotRule, rule_id)
        if rule and rule.account_id == account.id:
            await session.delete(rule)
            await session.commit()

    await call.answer("Правило удалено", show_alert=True)
    await roblox_rules_list(call)


# --- ИСТОРИЯ СДЕЛОК ---

@router.callback_query(F.data == "roblox_vc:deals:list")
async def roblox_deals_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        deals = (await session.scalars(
            select(RobloxDeal).where(
                RobloxDeal.account_id == account.id
            ).order_by(RobloxDeal.id.desc()).limit(10)
        )).all()

    lines = []
    for d in deals:
        dt = d.created_at.strftime("%d.%m %H:%M") if d.created_at else ""
        lines.append(
            f"• Логин: <code>{html.escape(d.username)}</code> | Сделка: <code>{d.deal_id}</code> ({dt})"
        )

    deals_text = "\n\n".join(lines) if lines else "<i>Сделок еще не было</i>"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="roblox_vc:open")],
    ])

    text = (
        f"📜 <b>Последние 10 автовыдач Roblox Voice Chat:</b>\n\n"
        f"{deals_text}"
    )

    await edit(call, text, markup)


@router.callback_query(F.data == "roblox_vc:clear_issued")
async def roblox_clear_issued(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        await session.execute(
            delete(RobloxStock).where(
                RobloxStock.account_id == account.id,
                RobloxStock.status == "issued",
            )
        )
        await session.commit()

    await call.answer("История выданных аккаунтов очищена", show_alert=True)
    await roblox_main_menu(call, None)  # type: ignore
