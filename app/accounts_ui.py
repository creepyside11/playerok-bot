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

from .accounts_manager import (
    DEFAULT_DELIVERY_TEMPLATE,
    PRESET_GAMES,
    UniversalAccountDeal,
    UniversalAccountLotRule,
    UniversalAccountStock,
    UniversalGame,
)
from .handlers import active_account, clip, edit, main_menu, require_account, svc
from .plugin_system import PluginState

logger = logging.getLogger("universal_accounts_ui")
router = Router(name="universal_accounts_ui")


class UniversalAccStates(StatesGroup):
    game_custom_name = State()
    account_add_text = State()
    lot_template_edit = State()


# --- ГЛАВНОЕ МЕНЮ АВТОВЫДАЧИ АККАУНТОВ ---

@router.callback_query(F.data == "uacc:open")
async def uacc_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        # Автоматически активируем плагин universal_accounts
        p_state = await session.scalar(
            select(PluginState).where(
                PluginState.account_id == account.id,
                PluginState.plugin_id == "universal_accounts",
            )
        )
        if not p_state:
            session.add(PluginState(account_id=account.id, plugin_id="universal_accounts", enabled=True, config={}))
            await session.commit()
        elif not p_state.enabled:
            p_state.enabled = True
            await session.commit()

        games_count = await session.scalar(
            select(func.count(UniversalGame.id)).where(UniversalGame.account_id == account.id)
        ) or 0
        stock_count = await session.scalar(
            select(func.count(UniversalAccountStock.id)).where(
                UniversalAccountStock.account_id == account.id,
                UniversalAccountStock.status == "available",
            )
        ) or 0
        rules_count = await session.scalar(
            select(func.count(UniversalAccountLotRule.id)).where(
                UniversalAccountLotRule.account_id == account.id,
                UniversalAccountLotRule.enabled.is_(True),
            )
        ) or 0

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🎮 Мои игры и приложения", callback_data="uacc:games:0"))
    builder.row(InlineKeyboardButton(text="➕ Добавить игру / категорию", callback_data="uacc:game:add_menu"))
    builder.row(InlineKeyboardButton(text="📋 Привязанные лоты Playerok", callback_data="uacc:rules:0"))
    builder.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:root"))

    text = (
        "🎮 <b>Универсальная автовыдача аккаунтов</b>\n\n"
        "Настраиваемая система автоматической выдачи для любых игр:\n"
        "<i>Black Russia, Brawl Stars, Standoff 2, Steam, Telegram, Roblox и др.</i>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Игр в системе: <b>{games_count}</b>\n"
        f"• Аккаунтов в наличии: <b>{stock_count} шт.</b>\n"
        f"• Привязано активных лотов: <b>{rules_count}</b>\n\n"
        "🟢 <i>Плагин активен. При покупке покупатель мгновенно получает данные в чат Playerok, а заказ отмечается выполненным.</i>"
    )
    await edit(call, text, builder.as_markup())


# --- СПИСОК ИГР ---

@router.callback_query(F.data.startswith("uacc:games:"))
async def uacc_games_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    page = int(call.data.split(":")[2])
    page_size = 6

    async with svc().db() as session:
        total = await session.scalar(
            select(func.count(UniversalGame.id)).where(UniversalGame.account_id == account.id)
        ) or 0
        games = (await session.scalars(
            select(UniversalGame)
            .where(UniversalGame.account_id == account.id)
            .order_by(UniversalGame.title)
            .offset(page * page_size)
            .limit(page_size)
        )).all()

    builder = InlineKeyboardBuilder()
    for g in games:
        builder.row(InlineKeyboardButton(text=f"🎮 {clip(g.title, 26)}", callback_data=f"uacc:game:view:{g.id}"))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"uacc:games:{page - 1}"))
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"uacc:games:{page + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="➕ Добавить игру", callback_data="uacc:game:add_menu"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="uacc:open"))

    await edit(call, f"🎮 <b>Список игр и приложений ({total}):</b>\nВыберите игру для управления складом и лотами:", builder.as_markup())


# --- ДОБАВЛЕНИЕ ИГРЫ ---

@router.callback_query(F.data == "uacc:game:add_menu")
async def uacc_game_add_menu(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    builder = InlineKeyboardBuilder()
    for p_name in PRESET_GAMES:
        builder.button(text=p_name, callback_data=f"uacc:game:add_preset:{p_name}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="✍️ Ввести своё название", callback_data="uacc:game:add_custom"))
    builder.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="uacc:open"))

    await edit(
        call,
        "➕ <b>Добавление игры / приложения</b>\n\n"
        "Выберите игру из популярных пресетов или введите собственное название:",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("uacc:game:add_preset:"))
async def uacc_game_add_preset(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    title = call.data.split(":", 3)[-1].strip()

    async with svc().db() as session:
        existing = await session.scalar(
            select(UniversalGame).where(
                UniversalGame.account_id == account.id,
                UniversalGame.title == title,
            )
        )
        if not existing:
            g = UniversalGame(
                account_id=account.id,
                title=title,
                slug=title.lower().replace(" ", "_"),
            )
            session.add(g)
            await session.commit()
            gid = g.id
        else:
            gid = existing.id

    await call.answer(f"Игра {title} добавлена!")
    await _render_game_view(call, account, gid)


@router.callback_query(F.data == "uacc:game:add_custom")
async def uacc_game_add_custom(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.set_state(UniversalAccStates.game_custom_name)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="uacc:open")]])
    await edit(call, "✍️ <b>Введите название игры или приложения:</b>\n<i>(Например: Black Russia, GTA 5, Valorant, Telegram)</i>", markup)


@router.message(UniversalAccStates.game_custom_name, F.text)
async def uacc_game_custom_save(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        return
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return

    async with svc().db() as session:
        existing = await session.scalar(
            select(UniversalGame).where(
                UniversalGame.account_id == account.id,
                UniversalGame.title == title,
            )
        )
        if not existing:
            g = UniversalGame(
                account_id=account.id,
                title=title,
                slug=title.lower().replace(" ", "_"),
            )
            session.add(g)
            await session.commit()
            gid = g.id
        else:
            gid = existing.id

    await state.clear()
    await message.answer(f"✅ Игра <b>{html.escape(title)}</b> успешно добавлена!", parse_mode="HTML")
    await _render_game_view(message, account, gid)


# --- КАРТОЧКА ИГРЫ ---

async def _render_game_view(target: CallbackQuery | Message, account: Any, game_id: int) -> None:
    async with svc().db() as session:
        game = await session.get(UniversalGame, game_id)
        if not game:
            if isinstance(target, CallbackQuery):
                await edit(target, "❌ Игра не найдена.", back_to_games())
            else:
                await target.answer("❌ Игра не найдена.", reply_markup=back_to_games())
            return

        avail = await session.scalar(
            select(func.count(UniversalAccountStock.id)).where(
                UniversalAccountStock.account_id == account.id,
                UniversalAccountStock.game_id == game_id,
                UniversalAccountStock.status == "available",
            )
        ) or 0
        issued = await session.scalar(
            select(func.count(UniversalAccountStock.id)).where(
                UniversalAccountStock.account_id == account.id,
                UniversalAccountStock.game_id == game_id,
                UniversalAccountStock.status == "issued",
            )
        ) or 0
        rules = (await session.scalars(
            select(UniversalAccountLotRule).where(
                UniversalAccountLotRule.account_id == account.id,
                UniversalAccountLotRule.game_id == game_id,
            )
        )).all()

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📥 Добавить аккаунты", callback_data=f"uacc:stock:add:{game_id}"),
        InlineKeyboardButton(text=f"📦 Склад ({avail} шт.)", callback_data=f"uacc:stock:list:{game_id}:0"),
    )
    builder.row(
        InlineKeyboardButton(text="🔗 Привязать лот Playerok", callback_data=f"uacc:lot:bind_menu:{game_id}:0"),
        InlineKeyboardButton(text=f"📋 Привязанные лоты ({len(rules)})", callback_data=f"uacc:rules_game:{game_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Удалить игру", callback_data=f"uacc:game:del_ask:{game_id}"),
        InlineKeyboardButton(text="⬅️ К списку игр", callback_data="uacc:games:0"),
    )

    text = (
        f"🎮 <b>Управление игрой: {html.escape(game.title)}</b>\n\n"
        f"📊 <b>Склад:</b>\n"
        f"• В наличии для выдачи: <b>{avail} шт.</b>\n"
        f"• Успешно выдано покупателям: <b>{issued} шт.</b>\n\n"
        f"🔗 <b>Привязанных лотов:</b> <b>{len(rules)}</b>\n\n"
        "<i>Загрузите аккаунты на склад и привяжите лоты Playerok, чтобы бот автоматически выдавал их при покупке.</i>"
    )

    if isinstance(target, CallbackQuery):
        await edit(target, text, builder.as_markup())
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("uacc:game:view:"))
async def uacc_game_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    gid = int(call.data.split(":")[3])
    await _render_game_view(call, account, gid)


# --- ДОБАВЛЕНИЕ АККАУНТОВ НА СКЛАД ---

@router.callback_query(F.data.startswith("uacc:stock:add:"))
async def uacc_stock_add_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    gid = int(call.data.split(":")[3])
    await state.clear()
    await state.set_state(UniversalAccStates.account_add_text)
    await state.update_data(game_id=gid)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"uacc:game:view:{gid}")]]
    )
    await edit(
        call,
        "📥 <b>Добавление аккаунтов на склад</b>\n\n"
        "Отправьте один или несколько аккаунтов (каждый с новой строки).\n\n"
        "📌 <b>Поддерживаемые форматы:</b>\n"
        "1) <code>логин:пароль</code>\n"
        "2) <code>логин:пароль:id_аккаунта</code>\n"
        "3) <code>логин:пароль:id:сервер:комментарий</code>\n"
        "4) Или блоками:\n"
        "<code>Логин\nПароль\nID / Никнейм\nИнструкция</code>\n\n"
        "<i>Все пароли и доступы шифруются военным шифрованием AES перед записью в базу.</i>",
        markup,
    )


@router.message(UniversalAccStates.account_add_text, F.text)
async def uacc_stock_add_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    gid = int(data.get("game_id", 0))
    account = await active_account(message.from_user.id)
    if not account or not gid:
        await state.clear()
        return

    text = (message.text or "").strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return

    cipher = svc().cipher
    added_count = 0

    async with svc().db() as session:
        # Проверяем формат одной записи блоком (4 строки) или построчно
        if len(lines) in (2, 3, 4) and ":" not in lines[0]:
            # Один аккаунт блоком
            login = lines[0]
            pwd = lines[1]
            acc_id = lines[2] if len(lines) > 2 else None
            comment = lines[3] if len(lines) > 3 else None
            item = UniversalAccountStock(
                account_id=account.id,
                game_id=gid,
                login=login,
                password_encrypted=cipher.encrypt(pwd),
                account_identifier=acc_id,
                comment=comment,
                status="available",
            )
            session.add(item)
            added_count += 1
        else:
            # Массовая загрузка (по строкам)
            for line in lines:
                parts = line.split(":")
                if len(parts) >= 2:
                    login = parts[0].strip()
                    pwd = parts[1].strip()
                    acc_id = parts[2].strip() if len(parts) > 2 else None
                    server = parts[3].strip() if len(parts) > 3 else None
                    comment = ":".join(parts[4:]).strip() if len(parts) > 4 else None
                    session.add(UniversalAccountStock(
                        account_id=account.id,
                        game_id=gid,
                        login=login,
                        password_encrypted=cipher.encrypt(pwd),
                        account_identifier=acc_id,
                        server=server,
                        comment=comment,
                        status="available",
                    ))
                    added_count += 1
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>Успешно добавлено аккаунтов на склад: {added_count} шт.</b>",
        parse_mode="HTML",
    )
    await _render_game_view(message, account, gid)


# --- СПИСОК АККАУНТОВ СКЛАДА ---

@router.callback_query(F.data.startswith("uacc:stock:list:"))
async def uacc_stock_list_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    parts = call.data.split(":")
    gid = int(parts[3])
    page = int(parts[4])
    page_size = 6

    async with svc().db() as session:
        game = await session.get(UniversalGame, gid)
        total = await session.scalar(
            select(func.count(UniversalAccountStock.id)).where(
                UniversalAccountStock.account_id == account.id,
                UniversalAccountStock.game_id == gid,
            )
        ) or 0
        stocks = (await session.scalars(
            select(UniversalAccountStock)
            .where(
                UniversalAccountStock.account_id == account.id,
                UniversalAccountStock.game_id == gid,
            )
            .order_by(UniversalAccountStock.id.desc())
            .offset(page * page_size)
            .limit(page_size)
        )).all()

    builder = InlineKeyboardBuilder()
    for s in stocks:
        icon = "🟢" if s.status == "available" else "🔵"
        builder.button(text=f"{icon} {clip(s.login, 20)}", callback_data=f"uacc:stock:view:{s.id}")
        builder.button(text="🗑", callback_data=f"uacc:stock:del:{s.id}:{gid}:{page}")
    builder.adjust(2)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"uacc:stock:list:{gid}:{page - 1}"))
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"uacc:stock:list:{gid}:{page + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="📥 Добавить ещё аккаунты", callback_data=f"uacc:stock:add:{gid}"))
    builder.row(InlineKeyboardButton(text=f"⬅️ Назад в {game.title if game else 'игру'}", callback_data=f"uacc:game:view:{gid}"))

    await edit(
        call,
        f"📦 <b>Склад аккаунтов {html.escape(game.title if game else '')} ({total} шт.):</b>\n"
        "🟢 — свободен к выдаче | 🔵 — выдан покупателю",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("uacc:stock:del:"))
async def uacc_stock_delete(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    parts = call.data.split(":")
    sid = int(parts[3])
    gid = int(parts[4])
    page = int(parts[5])

    async with svc().db() as session:
        await session.execute(
            delete(UniversalAccountStock).where(
                UniversalAccountStock.id == sid,
                UniversalAccountStock.account_id == account.id,
            )
        )
        await session.commit()

    await call.answer("Аккаунт удалён со склада.")
    call.data = f"uacc:stock:list:{gid}:{page}"
    await uacc_stock_list_view(call)


@router.callback_query(F.data.startswith("uacc:stock:view:"))
async def uacc_stock_detail(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    sid = int(call.data.split(":")[3])

    async with svc().db() as session:
        stock = await session.get(UniversalAccountStock, sid)
        if not stock:
            await call.answer("Аккаунт не найден.", show_alert=True)
            return
        game = await session.get(UniversalGame, stock.game_id)

    cipher = svc().cipher
    pwd = cipher.decrypt(stock.password_encrypted)

    st_str = "🟢 Свободен (готов к выдаче)" if stock.status == "available" else f"🔵 Выдан (сделка {stock.issued_deal_id})"
    extra_str = f"\n🌐 Сервер: <code>{html.escape(stock.server)}</code>" if stock.server else ""
    id_str = f"\n🆔 ID/Ник: <code>{html.escape(stock.account_identifier)}</code>" if stock.account_identifier else ""
    comm_str = f"\n📝 Комментарий: {html.escape(stock.comment)}" if stock.comment else ""

    text = (
        f"🔑 <b>Детали аккаунта {html.escape(game.title if game else '')}</b>\n\n"
        f"Статус: <b>{st_str}</b>\n"
        f"Логин: <code>{html.escape(stock.login)}</code>\n"
        f"Пароль: <code>{html.escape(pwd)}</code>"
        f"{id_str}{extra_str}{comm_str}\n"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"uacc:stock:del:{stock.id}:{stock.game_id}:0")],
        [InlineKeyboardButton(text="⬅️ Назад на склад", callback_data=f"uacc:stock:list:{stock.game_id}:0")],
    ])
    await edit(call, text, markup)


# --- ПРИВЯЗКА ЛОТОВ PLAYEROK ---

@router.callback_query(F.data.startswith("uacc:lot:bind_menu:"))
async def uacc_lot_bind_menu(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    parts = call.data.split(":")
    gid = int(parts[3])
    page = int(parts[4])
    page_size = 6

    await call.answer("Загружаю ваши лоты Playerok…")
    client = await svc().gateway.get_client(account)
    try:
        raw_items = await client.viewer()
        items = list(getattr(raw_items, "items", []) or [])
    except Exception:
        items = []

    builder = InlineKeyboardBuilder()
    start_idx = page * page_size
    page_items = items[start_idx : start_idx + page_size]

    for it in page_items:
        it_id = getattr(it, "id", "")
        it_name = getattr(it, "name", "Товар")
        builder.button(text=f"📦 {clip(it_name, 26)}", callback_data=f"uacc:lot:bind_confirm:{gid}:{it_id}")
    builder.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"uacc:lot:bind_menu:{gid}:{page - 1}"))
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if start_idx + page_size < len(items):
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"uacc:lot:bind_menu:{gid}:{page + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="🌟 Привязать ВСЕ лоты этой игры (*)", callback_data=f"uacc:lot:bind_confirm:{gid}:*"))
    builder.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"uacc:game:view:{gid}"))

    await edit(
        call,
        "🔗 <b>Привязка лота Playerok к автовыдаче</b>\n\n"
        "Выберите товар, при покупке которого бот автоматически выдаст аккаунт покупателю:",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("uacc:lot:bind_confirm:"))
async def uacc_lot_bind_confirm(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    parts = call.data.split(":")
    gid = int(parts[3])
    lot_id = parts[4]

    lot_title = f"Лот {lot_id}"
    if lot_id == "*":
        lot_title = "Все лоты игры"
    else:
        try:
            client = await svc().gateway.get_client(account)
            it = await client.get_item(lot_id)
            if it and getattr(it, "name", None):
                lot_title = it.name
        except Exception:
            pass

    async with svc().db() as session:
        existing = await session.scalar(
            select(UniversalAccountLotRule).where(
                UniversalAccountLotRule.account_id == account.id,
                UniversalAccountLotRule.lot_id == lot_id,
            )
        )
        if existing:
            existing.game_id = gid
            existing.lot_title = lot_title
            existing.enabled = True
        else:
            session.add(UniversalAccountLotRule(
                account_id=account.id,
                game_id=gid,
                lot_id=lot_id,
                lot_title=lot_title,
                delivery_template=DEFAULT_DELIVERY_TEMPLATE,
                auto_sent=True,
                enabled=True,
            ))
        await session.commit()

    await call.answer("Лот успешно привязан!")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 К привязанным лотам", callback_data=f"uacc:rules_game:{gid}")],
        [InlineKeyboardButton(text="🎮 В меню игры", callback_data=f"uacc:game:view:{gid}")],
    ])
    await edit(
        call,
        f"✅ <b>Лот успешно привязан к автовыдаче!</b>\n\n"
        f"📦 Товар: <b>{html.escape(lot_title)}</b>\n"
        f"🆔 ID лота: <code>{lot_id}</code>\n\n"
        "При оплате заказа бот автоматически выдаст аккаунт покупателю в чат Playerok!",
        markup,
    )


# --- СПИСОК ПРИВЯЗАННЫХ ЛОТОВ ---

@router.callback_query(F.data.startswith("uacc:rules:"))
@router.callback_query(F.data.startswith("uacc:rules_game:"))
async def uacc_rules_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    is_game = call.data.startswith("uacc:rules_game:")
    gid = int(call.data.split(":")[2]) if is_game else None

    async with svc().db() as session:
        query = select(UniversalAccountLotRule).where(UniversalAccountLotRule.account_id == account.id)
        if gid:
            query = query.where(UniversalAccountLotRule.game_id == gid)
        rules = (await session.scalars(query.order_by(UniversalAccountLotRule.id.desc()))).all()

    builder = InlineKeyboardBuilder()
    for r in rules:
        builder.button(text=f"📦 {clip(r.lot_title, 22)}", callback_data=f"uacc:rule:view:{r.id}")
        builder.button(text="🗑", callback_data=f"uacc:rule:del:{r.id}")
    builder.adjust(2)

    back_target = f"uacc:game:view:{gid}" if gid else "uacc:open"
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))

    title = "📋 <b>Привязанные лоты Playerok:</b>" if not gid else "📋 <b>Привязанные лоты игры:</b>"
    await edit(call, f"{title}\nКоличество: <b>{len(rules)}</b>", builder.as_markup())


@router.callback_query(F.data.startswith("uacc:rule:del:"))
async def uacc_rule_delete(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    rid = int(call.data.split(":")[3])
    async with svc().db() as session:
        await session.execute(
            delete(UniversalAccountLotRule).where(
                UniversalAccountLotRule.id == rid,
                UniversalAccountLotRule.account_id == account.id,
            )
        )
        await session.commit()
    await call.answer("Привязка лота удалена.")
    await uacc_main_menu(call, None)


@router.callback_query(F.data.startswith("uacc:rule:view:"))
async def uacc_rule_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    rid = int(call.data.split(":")[3])
    async with svc().db() as session:
        rule = await session.get(UniversalAccountLotRule, rid)
        if not rule:
            await call.answer("Правило не найдено.", show_alert=True)
            return
        game = await session.get(UniversalGame, rule.game_id)

    st_icon = "🟢 Включено" if rule.enabled else "🔴 Выключено"
    auto_sent_str = "Да (SENT)" if rule.auto_sent else "Нет"

    text = (
        f"⚙️ <b>Настройки автовыдачи лота</b>\n\n"
        f"📦 Товар: <b>{html.escape(rule.lot_title)}</b>\n"
        f"🆔 ID лота: <code>{rule.lot_id}</code>\n"
        f"🎮 Игра: <b>{html.escape(game.title if game else '—')}</b>\n"
        f"📌 Статус: <b>{st_icon}</b>\n"
        f"🚀 Авто-выполнение заказа: <b>{auto_sent_str}</b>\n\n"
        f"📝 <b>Шаблон сообщения:</b>\n"
        f"<pre>{html.escape(rule.delivery_template[:800])}</pre>"
    )

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔄 Переключить активность", callback_data=f"uacc:rule:toggle:{rule.id}"))
    builder.row(InlineKeyboardButton(text="🚀 Переключить автовыполнение (SENT)", callback_data=f"uacc:rule:toggle_sent:{rule.id}"))
    builder.row(InlineKeyboardButton(text="🗑 Удалить привязку", callback_data=f"uacc:rule:del:{rule.id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"uacc:rules_game:{rule.game_id}"))

    await edit(call, text, builder.as_markup())


@router.callback_query(F.data.startswith("uacc:rule:toggle:"))
async def uacc_rule_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    rid = int(call.data.split(":")[3])
    async with svc().db() as session:
        rule = await session.get(UniversalAccountLotRule, rid)
        if rule and rule.account_id == account.id:
            rule.enabled = not rule.enabled
            await session.commit()
    await call.answer("Статус правила обновлён.")
    call.data = f"uacc:rule:view:{rid}"
    await uacc_rule_view(call)


@router.callback_query(F.data.startswith("uacc:rule:toggle_sent:"))
async def uacc_rule_toggle_sent(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    rid = int(call.data.split(":")[3])
    async with svc().db() as session:
        rule = await session.get(UniversalAccountLotRule, rid)
        if rule and rule.account_id == account.id:
            rule.auto_sent = not rule.auto_sent
            await session.commit()
    await call.answer("Автовыполнение обновлено.")
    call.data = f"uacc:rule:view:{rid}"
    await uacc_rule_view(call)


@router.callback_query(F.data.startswith("uacc:game:del_ask:"))
async def uacc_game_delete_ask(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    gid = int(call.data.split(":")[3])
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Да, удалить вместе со складом", callback_data=f"uacc:game:del_confirm:{gid}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"uacc:game:view:{gid}")],
    ])
    await edit(call, "⚠️ <b>Вы уверены, что хотите удалить эту игру?</b>\nВсе невыданные аккаунты склада и привязки лотов также будут удалены.", markup)


@router.callback_query(F.data.startswith("uacc:game:del_confirm:"))
async def uacc_game_delete_confirm(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    gid = int(call.data.split(":")[3])
    async with svc().db() as session:
        await session.execute(delete(UniversalGame).where(UniversalGame.id == gid, UniversalGame.account_id == account.id))
        await session.commit()
    await call.answer("Игра удалена.")
    call.data = "uacc:games:0"
    await uacc_games_list(call)


def back_to_games() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К списку игр", callback_data="uacc:games:0")]])
