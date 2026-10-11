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
from .supercell_manager import (
    DEFAULT_SUPERCELL_DELIVERY_TEMPLATE,
    SUPERCELL_GAMES,
    SupercellDeal,
    SupercellLotRule,
    SupercellStock,
    format_supercell_delivery_message,
    parse_supercell_bulk_lines,
)

logger = logging.getLogger("supercell_ui")
router = Router(name="supercell_ui")


class SupercellStates(StatesGroup):
    stock_add_lines = State()
    lot_template_edit = State()


# --- ГЛАВНОЕ МЕНЮ SUPERCELL ---

@router.callback_query(F.data == "supercell:open")
async def supercell_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        # Автоматически активируем плагин supercell_seller
        p_state = await session.scalar(
            select(PluginState).where(
                PluginState.account_id == account.id,
                PluginState.plugin_id == "supercell_seller",
            )
        )
        if not p_state:
            session.add(PluginState(account_id=account.id, plugin_id="supercell_seller", enabled=True, config={}))
            await session.commit()
        elif not p_state.enabled:
            p_state.enabled = True
            await session.commit()

        # Считаем общее количество аккаунтов на складе и выданных
        total_available = (await session.scalar(
            select(func.count(SupercellStock.id)).where(
                SupercellStock.account_id == account.id,
                SupercellStock.status == "available",
            )
        )) or 0
        total_issued = (await session.scalar(
            select(func.count(SupercellStock.id)).where(
                SupercellStock.account_id == account.id,
                SupercellStock.status == "issued",
            )
        )) or 0

        # Количество привязанных правил
        rules_count = (await session.scalar(
            select(func.count(SupercellLotRule.id)).where(SupercellLotRule.account_id == account.id)
        )) or 0

        # Считаем остатки по каждой игре
        game_stats = {}
        for g_key in SUPERCELL_GAMES:
            c = (await session.scalar(
                select(func.count(SupercellStock.id)).where(
                    SupercellStock.account_id == account.id,
                    SupercellStock.game_key == g_key,
                    SupercellStock.status == "available",
                )
            )) or 0
            game_stats[g_key] = c

    builder = InlineKeyboardBuilder()

    # Кнопки для каждой игры Supercell
    for g_key, g_name in SUPERCELL_GAMES.items():
        cnt = game_stats.get(g_key, 0)
        builder.button(
            text=f"{g_name} ({cnt} шт.)",
            callback_data=f"sc_game:{g_key}",
        )
    builder.adjust(1)

    builder.row(
        InlineKeyboardButton(text="🔗 Привязка лотов Playerok", callback_data="sc_rules:list"),
    )
    builder.row(
        InlineKeyboardButton(text="📜 История сделок", callback_data="sc_deals:list"),
        InlineKeyboardButton(text="🗑 Очистить выданные", callback_data="sc_stock:clear_issued"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Меню плагинов", callback_data="plugins:mine"),
    )

    text = (
        "⚡ <b>Автовыдача аккаунтов Supercell ID</b>\n\n"
        "Специализированная выдача аккаунтов для <b>Brawl Stars</b>, "
        "<b>Clash Royale</b>, <b>Clash of Clans</b> и других игр.\n\n"
        f"📦 Доступно на складе: <b>{total_available} шт.</b>\n"
        f"✅ Успешно выдано: <b>{total_issued} шт.</b>\n"
        f"🔗 Активных привязок: <b>{rules_count} шт.</b>\n\n"
        "<i>Выберите игру для добавления/просмотра аккаунтов:</i>"
    )

    await edit(call, text, builder.as_markup())


# --- УПРАВЛЕНИЕ КОНКРЕТНОЙ ИГРОЙ ---

@router.callback_query(F.data.startswith("sc_game:"))
async def supercell_game_detail(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    game_key = call.data.split(":", 1)[1]
    game_name = SUPERCELL_GAMES.get(game_key, game_key)

    async with svc().db() as session:
        available = (await session.scalar(
            select(func.count(SupercellStock.id)).where(
                SupercellStock.account_id == account.id,
                SupercellStock.game_key == game_key,
                SupercellStock.status == "available",
            )
        )) or 0
        issued = (await session.scalar(
            select(func.count(SupercellStock.id)).where(
                SupercellStock.account_id == account.id,
                SupercellStock.game_key == game_key,
                SupercellStock.status == "issued",
            )
        )) or 0

        # Получаем последние 5 доступных для предпросмотра
        recent = (await session.scalars(
            select(SupercellStock).where(
                SupercellStock.account_id == account.id,
                SupercellStock.game_key == game_key,
                SupercellStock.status == "available",
            ).order_by(SupercellStock.id.desc()).limit(5)
        )).all()

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="➕ Добавить аккаунты", callback_data=f"sc_add:{game_key}"),
        InlineKeyboardButton(text="📥 Выгрузить базу .txt", callback_data=f"sc_export:{game_key}"),
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Очистить склад игры", callback_data=f"sc_clear_game:{game_key}"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад к играм", callback_data="supercell:open"),
    )

    preview_lines = []
    for s in recent:
        trophies_info = f" ({s.trophies:,} 🏆)" if s.trophies is not None else ""
        th_info = f" (ТХ {s.town_hall})" if s.town_hall is not None else ""
        br_info = f" ({s.brawlers_count} 🥊)" if s.brawlers_count is not None else ""
        preview_lines.append(f"• <code>{html.escape(s.email)}</code>{trophies_info}{th_info}{br_info}")

    preview_text = "\n".join(preview_lines) if preview_lines else "<i>Склад пуст</i>"

    text = (
        f"🎮 <b>Управление: {html.escape(game_name)}</b>\n\n"
        f"📦 В наличии: <b>{available} шт.</b>\n"
        f"✅ Продано: <b>{issued} шт.</b>\n\n"
        f"<b>Последние добавленные:</b>\n{preview_text}\n\n"
        "<i>Нажмите «➕ Добавить аккаунты», чтобы загрузить базу почт и паролей.</i>"
    )

    await edit(call, text, builder.as_markup())


# --- ДОБАВЛЕНИЕ АККАУНТОВ ---

@router.callback_query(F.data.startswith("sc_add:"))
async def supercell_add_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return

    game_key = call.data.split(":", 1)[1]
    game_name = SUPERCELL_GAMES.get(game_key, game_key)

    await state.update_data(game_key=game_key)
    await state.set_state(SupercellStates.stock_add_lines)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"sc_game:{game_key}")],
    ])

    text = (
        f"➕ <b>Добавление аккаунтов в {html.escape(game_name)}</b>\n\n"
        "Отправьте данные аккаунтов сообщением (по одной строке на аккаунт):\n\n"
        "<b>Форматы строк:</b>\n"
        "<code>email:password</code>\n"
        "<code>email:password:коды_восстановления</code>\n"
        "<code>email:password:коды:комментарий/кубки</code>\n\n"
        "<i>Разделителем может быть двоеточие <code>:</code>, точка с запятой <code>;</code> или знак табуляции.</i>"
    )

    await edit(call, text, markup)


@router.message(SupercellStates.stock_add_lines)
async def supercell_add_process(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    game_key = data.get("game_key", "brawl_stars")
    game_name = SUPERCELL_GAMES.get(game_key, game_key)

    account = await active_account(message.from_user.id)
    if not account:
        await message.answer("Сначала выберите или добавьте аккаунт Playerok.")
        await state.clear()
        return

    text = (message.text or "").strip()
    parsed = parse_supercell_bulk_lines(text)
    if not parsed:
        await message.answer(
            "⚠️ Не удалось распознать аккаунты. Убедитесь, что формат <code>email:password</code> (хотя бы логин и пароль в строке)."
        )
        return

    cipher = svc().cipher
    added_count = 0

    async with svc().db() as session:
        for item in parsed:
            email = item["email"]
            raw_pwd = item["password"]
            raw_rec = item.get("recovery_codes")
            comment = item.get("comment")

            # Пробуем распознать кубки / уровень ТХ из комментария, если там число
            trophies = None
            town_hall = None
            brawlers = None
            if comment and comment.isdigit():
                val = int(comment)
                if game_key == "clash_of_clans" and val <= 20:
                    town_hall = val
                elif game_key == "brawl_stars" and val <= 100:
                    brawlers = val
                else:
                    trophies = val

            pwd_enc = cipher.encrypt(raw_pwd) if cipher else raw_pwd
            rec_enc = cipher.encrypt(raw_rec) if (cipher and raw_rec) else raw_rec

            stock = SupercellStock(
                account_id=account.id,
                game_key=game_key,
                email=email,
                password_encrypted=pwd_enc,
                recovery_codes_encrypted=rec_enc,
                trophies=trophies,
                town_hall=town_hall,
                brawlers_count=brawlers,
                comment=comment,
                status="available",
            )
            session.add(stock)
            added_count += 1

        await session.commit()

    await state.clear()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎮 В меню {game_name}", callback_data=f"sc_game:{game_key}")],
        [InlineKeyboardButton(text="⚡ Главная Supercell", callback_data="supercell:open")],
    ])

    await message.answer(
        f"✅ <b>Успешно добавлено {added_count} аккаунтов</b> в категорию <b>{html.escape(game_name)}</b>!",
        reply_markup=markup,
        parse_mode="HTML",
    )


# --- ЭКСПОРТ БАЗЫ В TXT ---

@router.callback_query(F.data.startswith("sc_export:"))
async def supercell_export_file(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    game_key = call.data.split(":", 1)[1]
    game_name = SUPERCELL_GAMES.get(game_key, game_key)

    cipher = svc().cipher

    async with svc().db() as session:
        stocks = (await session.scalars(
            select(SupercellStock).where(
                SupercellStock.account_id == account.id,
                SupercellStock.game_key == game_key,
                SupercellStock.status == "available",
            ).order_by(SupercellStock.id.asc())
        )).all()

    if not stocks:
        await call.answer("На складе этой игры нет доступных аккаунтов для выгрузки.", show_alert=True)
        return

    lines = []
    for s in stocks:
        pwd = cipher.decrypt(s.password_encrypted) if cipher else s.password_encrypted
        rec = ""
        if s.recovery_codes_encrypted:
            rec = cipher.decrypt(s.recovery_codes_encrypted) if cipher else s.recovery_codes_encrypted

        extra = f":{rec}" if rec else ""
        comm = f":{s.comment}" if s.comment else ""
        lines.append(f"{s.email}:{pwd}{extra}{comm}")

    file_bytes = "\n".join(lines).encode("utf-8")
    doc = io.BytesIO(file_bytes)
    doc.name = f"supercell_{game_key}_{len(stocks)}_accs.txt"

    from aiogram.types import BufferedInputFile
    input_file = BufferedInputFile(file_bytes, filename=doc.name)

    await call.message.answer_document(
        input_file,
        caption=f"📥 Выгрузка склада <b>{html.escape(game_name)}</b> ({len(stocks)} шт.)",
        parse_mode="HTML",
    )
    await call.answer()


# --- ОЧИСТКА СКЛАДА ИГРЫ ---

@router.callback_query(F.data.startswith("sc_clear_game:"))
async def supercell_clear_game_confirm(call: CallbackQuery) -> None:
    game_key = call.data.split(":", 1)[1]
    game_name = SUPERCELL_GAMES.get(game_key, game_key)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Да, удалить все аккаунты", callback_data=f"sc_clear_game_yes:{game_key}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"sc_game:{game_key}")],
    ])

    await edit(
        call,
        f"❓ <b>Подтверждение удаления:</b>\n\n"
        f"Вы действительно хотите удалить все невыданные аккаунты из <b>{html.escape(game_name)}</b>?",
        markup,
    )


@router.callback_query(F.data.startswith("sc_clear_game_yes:"))
async def supercell_clear_game_exec(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    game_key = call.data.split(":", 1)[1]

    async with svc().db() as session:
        await session.execute(
            delete(SupercellStock).where(
                SupercellStock.account_id == account.id,
                SupercellStock.game_key == game_key,
                SupercellStock.status == "available",
            )
        )
        await session.commit()

    await call.answer("Склад очищен", show_alert=True)
    await supercell_game_detail(call, None)  # type: ignore


# --- ПРИВЯЗКА ЛОТОВ PLAYEROK ---

@router.callback_query(F.data == "sc_rules:list")
async def supercell_rules_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        rules = (await session.scalars(
            select(SupercellLotRule).where(
                SupercellLotRule.account_id == account.id
            ).order_by(SupercellLotRule.id.desc())
        )).all()

    builder = InlineKeyboardBuilder()
    for r in rules:
        status_icon = "🟢" if r.enabled else "🔴"
        g_name = SUPERCELL_GAMES.get(r.game_key, r.game_key)
        builder.button(
            text=f"{status_icon} [{g_name}] {clip(r.lot_title, 25)}",
            callback_data=f"sc_rule_view:{r.id}",
        )
    builder.adjust(1)

    builder.row(
        InlineKeyboardButton(text="➕ Привязать лот Playerok", callback_data="sc_rule_add:pick_game"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад в меню Supercell", callback_data="supercell:open"),
    )

    text = (
        "🔗 <b>Привязанные лоты Playerok к автовыдаче Supercell:</b>\n\n"
        f"Всего правил: <b>{len(rules)} шт.</b>\n\n"
        "Когда покупатель оплачивает привязанный лот, бот берет аккаунт соответствующей игры, "
        "выдает его в чат и автоматически завершает сделку."
    )

    await edit(call, text, builder.as_markup())


@router.callback_query(F.data == "sc_rule_add:pick_game")
async def supercell_rule_add_pick_game(call: CallbackQuery) -> None:
    builder = InlineKeyboardBuilder()
    for g_key, g_name in SUPERCELL_GAMES.items():
        builder.button(
            text=g_name,
            callback_data=f"sc_rule_add_game:{g_key}",
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="sc_rules:list"))

    await edit(
        call,
        "🎮 <b>Шаг 1 из 2: Выберите игру для привязки лота:</b>",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("sc_rule_add_game:"))
async def supercell_rule_add_select_lot(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    game_key = call.data.split(":", 1)[1]
    game_name = SUPERCELL_GAMES.get(game_key, game_key)

    # Получаем товары пользователя с Playerok
    client = await svc().gateway.client_for(account)
    items = []
    if client:
        try:
            items = await client.get_items()
        except Exception as exc:
            logger.warning("Could not fetch playerok items for supercell rule: %s", exc)

    builder = InlineKeyboardBuilder()

    # Универсальное правило для всех лотов
    builder.button(
        text=f"🌐 Все лоты (*) -> {game_name}",
        callback_data=f"sc_rule_create:{game_key}:all_lots:Все лоты",
    )

    for it in (items or [])[:20]:
        it_id = str(getattr(it, "id", "") or "")
        it_name = str(getattr(it, "name", "") or "Товар")
        if it_id:
            builder.button(
                text=f"📦 {clip(it_name, 30)}",
                callback_data=f"sc_rule_create:{game_key}:{it_id}:{it_name[:25]}",
            )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="sc_rules:list"))

    text = (
        f"📦 <b>Шаг 2 из 2: Выберите лот для {html.escape(game_name)}:</b>\n\n"
        "Выберите конкретный товар из вашего профиля Playerok или привяжите все заказы сразу."
    )

    await edit(call, text, builder.as_markup())


@router.callback_query(F.data.startswith("sc_rule_create:"))
async def supercell_rule_create_save(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    parts = call.data.split(":", 3)
    game_key = parts[1]
    lot_id = parts[2]
    lot_title = parts[3] if len(parts) > 3 else "Товар"

    if lot_id == "all_lots":
        lot_id = "*"

    async with svc().db() as session:
        # Проверяем существующее
        existing = await session.scalar(
            select(SupercellLotRule).where(
                SupercellLotRule.account_id == account.id,
                SupercellLotRule.lot_id == lot_id,
            )
        )
        if existing:
            existing.game_key = game_key
            existing.lot_title = lot_title
            existing.enabled = True
        else:
            rule = SupercellLotRule(
                account_id=account.id,
                game_key=game_key,
                lot_id=lot_id,
                lot_title=lot_title,
                delivery_template=DEFAULT_SUPERCELL_DELIVERY_TEMPLATE,
                auto_sent=True,
                enabled=True,
            )
            session.add(rule)
        await session.commit()

    await call.answer("Лот успешно привязан!", show_alert=True)
    await supercell_rules_list(call)


@router.callback_query(F.data.startswith("sc_rule_view:"))
async def supercell_rule_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.split(":", 1)[1])
    async with svc().db() as session:
        rule = await session.get(SupercellLotRule, rule_id)
        if not rule or rule.account_id != account.id:
            await call.answer("Правило не найдено", show_alert=True)
            return

    game_name = SUPERCELL_GAMES.get(rule.game_key, rule.game_key)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔴 Выключить" if rule.enabled else "🟢 Включить",
            callback_data=f"sc_rule_toggle:{rule.id}",
        ),
        InlineKeyboardButton(
            text="🤖 Авто-SENT: ВКЛ" if rule.auto_sent else "🤖 Авто-SENT: ВЫКЛ",
            callback_data=f"sc_rule_autosent:{rule.id}",
        ),
    )
    builder.row(
        InlineKeyboardButton(text="✏️ Шаблон выдачи", callback_data=f"sc_rule_tmpl:{rule.id}"),
        InlineKeyboardButton(text="🗑 Удалить правило", callback_data=f"sc_rule_del:{rule.id}"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ К списку привязок", callback_data="sc_rules:list"),
    )

    text = (
        f"⚙️ <b>Настройка привязки лота:</b>\n\n"
        f"🎮 Игра: <b>{html.escape(game_name)}</b>\n"
        f"📦 Лот: <b>{html.escape(rule.lot_title)}</b> (ID: <code>{rule.lot_id}</code>)\n"
        f"Статус: <b>{'🟢 Активно' if rule.enabled else '🔴 Отключено'}</b>\n"
        f"Авто-завершение заказа: <b>{'Да (SENT)' if rule.auto_sent else 'Нет'}</b>\n\n"
        f"<b>Текущий шаблон выдачи:</b>\n"
        f"<code>{html.escape(rule.delivery_template[:300])}...</code>"
    )

    await edit(call, text, builder.as_markup())


@router.callback_query(F.data.startswith("sc_rule_toggle:"))
async def supercell_rule_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.split(":", 1)[1])
    async with svc().db() as session:
        rule = await session.get(SupercellLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.enabled = not rule.enabled
            await session.commit()

    await call.answer("Статус изменен")
    await supercell_rule_view(call)


@router.callback_query(F.data.startswith("sc_rule_autosent:"))
async def supercell_rule_autosent(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.split(":", 1)[1])
    async with svc().db() as session:
        rule = await session.get(SupercellLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.auto_sent = not rule.auto_sent
            await session.commit()

    await call.answer("Режим автозавершения изменен")
    await supercell_rule_view(call)


@router.callback_query(F.data.startswith("sc_rule_del:"))
async def supercell_rule_del(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.split(":", 1)[1])
    async with svc().db() as session:
        rule = await session.get(SupercellLotRule, rule_id)
        if rule and rule.account_id == account.id:
            await session.delete(rule)
            await session.commit()

    await call.answer("Правило удалено", show_alert=True)
    await supercell_rules_list(call)


# --- ИСТОРИЯ СДЕЛОК ---

@router.callback_query(F.data == "sc_deals:list")
async def supercell_deals_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        deals = (await session.scalars(
            select(SupercellDeal).where(
                SupercellDeal.account_id == account.id
            ).order_by(SupercellDeal.id.desc()).limit(10)
        )).all()

    lines = []
    for d in deals:
        g_name = SUPERCELL_GAMES.get(d.game_key, d.game_key)
        dt = d.created_at.strftime("%d.%m %H:%M") if d.created_at else ""
        lines.append(
            f"• <b>{html.escape(g_name)}</b> | Сделка: <code>{d.deal_id}</code>\n"
            f"  Почта: <code>{html.escape(d.email)}</code> ({dt})"
        )

    deals_text = "\n\n".join(lines) if lines else "<i>Сделок еще не было</i>"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="supercell:open")],
    ])

    text = (
        f"📜 <b>Последние 10 автовыдач Supercell:</b>\n\n"
        f"{deals_text}"
    )

    await edit(call, text, markup)


@router.callback_query(F.data == "sc_stock:clear_issued")
async def supercell_stock_clear_issued(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        res = await session.execute(
            delete(SupercellStock).where(
                SupercellStock.account_id == account.id,
                SupercellStock.status == "issued",
            )
        )
        await session.commit()

    await call.answer("История выданных аккаунтов со склада очищена", show_alert=True)
    await supercell_main_menu(call, None)  # type: ignore
