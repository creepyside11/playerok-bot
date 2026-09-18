from __future__ import annotations

import asyncio
import html
import uuid
from collections import defaultdict
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from playerokapi.enums import ChatTypes, ItemDealDirections

from .handlers import active_account, clip, edit, require_account, svc
from .keyboards import back_menu, main_menu
from .models import AutoReplyRule, DeliveryRule, DeliveryStock, PlayerokAccount
from .plugin_system import PluginManager
from .states import (
    ChatReply,
    DeliveryStockAdd,
    ItemCatalog,
    ItemCreate,
    PluginSettingEdit,
)


router = Router(name="advanced")
_plugins: PluginManager | None = None


def configure_advanced(plugin_manager: PluginManager) -> Router:
    global _plugins
    _plugins = plugin_manager
    return router


def plugins() -> PluginManager:
    if _plugins is None:
        raise RuntimeError("Plugin manager is not configured")
    return _plugins


def _user_name(chat: Any, own_id: str) -> str:
    users = list(getattr(chat, "users", []) or [])
    other = next((u for u in users if str(getattr(u, "id", "")) != own_id), None)
    return str(getattr(other, "username", None) or "Диалог")


def _settings_back(plugin_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К плагину", callback_data=f"plugin:view:{plugin_index}")]
    ])


@router.callback_query(F.data == "menu:profile")
async def enhanced_profile(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer("Обновляю…")
    try:
        client = await svc().gateway.get_client(account)
        viewer, items, chats, deals = await asyncio.gather(
            client.viewer(),
            client.call("get_my_items", statuses=None, count=1),
            client.get_chats(type=ChatTypes.PM, count=1),
            client.get_deals(direction=ItemDealDirections.OUT, count=1),
        )
        enabled_plugins = await plugins().count_enabled(account.id)
        text = (
            f"👤 <b>{html.escape(viewer.username)}</b>\n"
            f"ID: <code>{html.escape(viewer.id)}</code>\n"
            f"Email: <code>{html.escape(viewer.email or '—')}</code>\n\n"
            f"💰 Баланс: <b>{html.escape(str(viewer.balance if viewer.balance is not None else '—'))} ₽</b>\n"
            f"⭐ Отзывы: <b>{viewer.testimonials if viewer.testimonials is not None else '—'}</b>\n"
            f"📦 Товаров: <b>{getattr(items, 'total_count', '—')}</b>\n"
            f"💬 Чатов: <b>{getattr(chats, 'total_count', '—')}</b>\n"
            f"🛒 Продаж: <b>{getattr(deals, 'total_count', '—')}</b>\n"
            f"🧩 Плагинов включено: <b>{enabled_plugins}</b>\n\n"
            f"Продажи: <b>{'разрешены' if viewer.can_publish_items else 'запрещены'}</b>\n"
            f"Блокировка: <b>{'есть' if viewer.is_blocked else 'нет'}</b>"
        )
    except Exception as exc:
        text = f"❌ Ошибка профиля:\n<code>{html.escape(str(exc))[:1700]}</code>"
    await edit(call, text, back_menu())


@router.callback_query(F.data == "menu:items")
async def enhanced_items(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await require_account(call):
        return
    await call.answer()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выставить товар", callback_data="catalog:start")],
        [InlineKeyboardButton(text="🔎 Найти игру", callback_data="catalog:search")],
        [InlineKeyboardButton(text="📚 Категория → игра", callback_data="catalog:categories")],
        [InlineKeyboardButton(text="📋 Мои товары", callback_data="items:list")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main")],
    ])
    await edit(
        call,
        "📦 <b>Товары</b>\n\nМожно выбрать сначала категорию, затем игру, "
        "или найти игру поиском по названию.",
        markup,
    )


@router.callback_query(F.data == "catalog:start")
async def catalog_start(call: CallbackQuery) -> None:
    await call.answer()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Категория → игра", callback_data="catalog:categories")],
        [InlineKeyboardButton(text="🔎 Поиск игры", callback_data="catalog:search")],
        [InlineKeyboardButton(text="⬅️ Товары", callback_data="menu:items")],
    ])
    await edit(call, "Как найти товарную категорию?", markup)


async def _load_catalog(account: PlayerokAccount) -> list[tuple[str, str, str, str, str]]:
    client = await svc().gateway.get_client(account)
    cursor = None
    rows: list[tuple[str, str, str, str, str]] = []
    for _ in range(20):
        page = await client.call("get_games", count=24, after_cursor=cursor)
        for game in list(getattr(page, "games", []) or []):
            for category in list(getattr(game, "categories", []) or []):
                rows.append((
                    str(getattr(category, "id", "")),
                    str(getattr(category, "name", "Категория")),
                    str(getattr(game, "id", "")),
                    str(getattr(game, "name", "Игра")),
                    str(getattr(game, "slug", "")),
                ))
        page_info = getattr(page, "page_info", None)
        if not page_info or not getattr(page_info, "has_next_page", False):
            break
        cursor = getattr(page_info, "end_cursor", None)
        if not cursor:
            break
    return rows


@router.callback_query(F.data == "catalog:categories")
async def catalog_categories(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer("Загружаю каталог…")
    try:
        rows = await _load_catalog(account)
    except Exception as exc:
        await edit(call, f"❌ <code>{html.escape(str(exc))[:1600]}</code>", back_menu("items"))
        return
    grouped: dict[str, list[tuple[str, str, str, str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row[1]].append(row)
    names = sorted(grouped, key=str.casefold)
    await state.update_data(catalog_rows=rows, catalog_category_names=names, account_id=str(account.id))
    b = InlineKeyboardBuilder()
    for i, name in enumerate(names[:45]):
        b.button(text=clip(name, 38), callback_data=f"catalog:cat:{i}")
    b.button(text="🔎 Поиск категории", callback_data="catalog:catsearch")
    b.button(text="⬅️ Товары", callback_data="menu:items")
    b.adjust(1)
    await edit(call, f"📚 <b>Категории</b>\nНайдено: {len(names)}", b.as_markup())


@router.callback_query(F.data == "catalog:catsearch")
async def category_search_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ItemCatalog.category_search)
    await call.answer()
    await edit(call, "Введите часть названия категории:", back_menu("items"))


@router.message(ItemCatalog.category_search)
async def category_search_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    query = (message.text or "").strip().casefold()
    names = [x for x in data.get("catalog_category_names", []) if query in x.casefold()]
    if not names:
        await message.answer("Ничего не найдено. Попробуйте другой запрос.")
        return
    source = data.get("catalog_category_names", [])
    b = InlineKeyboardBuilder()
    for name in names[:30]:
        b.button(text=clip(name, 38), callback_data=f"catalog:cat:{source.index(name)}")
    b.adjust(1)
    await state.set_state(ItemCreate.game)
    await message.answer("Выберите категорию:", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("catalog:cat:"))
async def catalog_category_games(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(call.data.rsplit(":", 1)[-1])
    names = data.get("catalog_category_names", [])
    if idx >= len(names):
        await call.answer("Категория устарела", show_alert=True)
        return
    name = names[idx]
    rows = [r for r in data.get("catalog_rows", []) if r[1] == name]
    unique: dict[str, tuple[str, str, str, str, str]] = {r[2]: r for r in rows}
    games = list(unique.values())
    await state.update_data(catalog_selected_rows=games)
    b = InlineKeyboardBuilder()
    for i, row in enumerate(games[:40]):
        b.button(text=clip(row[3], 38), callback_data=f"catalog:game:{i}")
    b.button(text="⬅️ Категории", callback_data="catalog:categories")
    b.adjust(1)
    await call.answer()
    await edit(call, f"🎮 Игры с категорией <b>{html.escape(name)}</b>:", b.as_markup())


@router.callback_query(F.data.startswith("catalog:game:"))
async def catalog_choose_game(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(call.data.rsplit(":", 1)[-1])
    rows = data.get("catalog_selected_rows", [])
    if idx >= len(rows):
        await call.answer("Список устарел", show_alert=True)
        return
    category_id, category_name, game_id, game_name, game_slug = rows[idx]
    account = await active_account(call.from_user.id)
    if not account:
        return
    try:
        game = await (await svc().gateway.get_client(account)).call("get_game", id=game_id)
    except Exception as exc:
        await call.answer("Ошибка API", show_alert=True)
        await call.message.answer(f"<code>{html.escape(str(exc))[:1200]}</code>", parse_mode="HTML")
        return
    categories = [(str(c.id), str(c.name)) for c in list(getattr(game, "categories", []) or [])]
    selected_index = next((i for i, x in enumerate(categories) if x[0] == category_id), None)
    await state.update_data(
        categories=categories,
        selected_game_name=game_name,
        selected_game_slug=game_slug,
        account_id=str(account.id),
    )
    await state.set_state(ItemCreate.category)
    b = InlineKeyboardBuilder()
    if selected_index is not None:
        b.button(text=f"✅ {clip(category_name, 34)}", callback_data=f"itemcat:{selected_index}")
    for i, (_cid, cname) in enumerate(categories):
        if i == selected_index:
            continue
        b.button(text=clip(cname, 38), callback_data=f"itemcat:{i}")
    b.adjust(1)
    await call.answer()
    await edit(call, f"🎮 <b>{html.escape(game_name)}</b>\nВыберите категорию:", b.as_markup())


@router.callback_query(F.data == "catalog:search")
async def game_search_start(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.set_state(ItemCatalog.game_search)
    await state.update_data(account_id=str(account.id))
    await call.answer()
    await edit(call, "🔎 Введите название игры или приложения:", back_menu("items"))


@router.message(ItemCatalog.game_search)
async def game_search_value(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        return
    query = (message.text or "").strip()
    try:
        page = await (await svc().gateway.get_client(account)).call("get_games", name=query, count=24)
        games = list(getattr(page, "games", []) or [])
    except Exception as exc:
        await message.answer(f"❌ <code>{html.escape(str(exc))[:1400]}</code>", parse_mode="HTML")
        return
    if not games:
        await message.answer("Игры не найдены.")
        return
    packed = [(str(g.id), str(g.name), str(g.slug)) for g in games]
    await state.update_data(search_games=packed)
    b = InlineKeyboardBuilder()
    for i, (_gid, name, _slug) in enumerate(packed):
        b.button(text=clip(name, 38), callback_data=f"catalog:searchgame:{i}")
    b.adjust(1)
    await message.answer("Результаты поиска:", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("catalog:searchgame:"))
async def search_game_choose(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(call.data.rsplit(":", 1)[-1])
    games = data.get("search_games", [])
    if idx >= len(games):
        return
    game_id, game_name, slug = games[idx]
    account = await active_account(call.from_user.id)
    game = await (await svc().gateway.get_client(account)).call("get_game", id=game_id)
    categories = [(str(c.id), str(c.name)) for c in list(getattr(game, "categories", []) or [])]
    await state.update_data(categories=categories, selected_game_name=game_name, selected_game_slug=slug)
    await state.set_state(ItemCreate.category)
    b = InlineKeyboardBuilder()
    for i, (_id, name) in enumerate(categories):
        b.button(text=clip(name, 38), callback_data=f"itemcat:{i}")
    b.adjust(1)
    await call.answer()
    await edit(call, f"🎮 <b>{html.escape(game_name)}</b>\nВыберите категорию:", b.as_markup())


async def _show_autoreplies(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        rules = list((await session.scalars(
            select(AutoReplyRule).where(AutoReplyRule.account_id == account.id).order_by(AutoReplyRule.id)
        )).all())
    b = InlineKeyboardBuilder()
    for rule in rules:
        icon = "✅" if rule.enabled else "⏸"
        label = "все сообщения" if rule.trigger == "*" else rule.trigger
        b.button(text=f"{icon} {clip(label, 30)}", callback_data=f"ar:view:{rule.id}")
    b.button(text="➕ Добавить правило", callback_data="autoreply:add")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    await edit(call, f"💬 <b>Автоответчик</b>\nПравил: <b>{len(rules)}</b>", b.as_markup())


@router.callback_query(F.data == "menu:autoreply")
async def autoreply_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _show_autoreplies(call)


@router.callback_query(F.data.startswith("ar:view:"))
async def autoreply_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    rid = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(AutoReplyRule, rid)
    if not rule or rule.account_id != account.id:
        return
    b = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏸ Выключить" if rule.enabled else "▶️ Включить",
            callback_data=f"ar:toggle:{rid}",
        )],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ar:remove:{rid}")],
        [InlineKeyboardButton(text="⬅️ Автоответчик", callback_data="menu:autoreply")],
    ])
    await call.answer()
    await edit(
        call,
        f"💬 <b>Правило #{rid}</b>\n"
        f"Триггер: <code>{html.escape(rule.trigger)}</code>\n"
        f"Статус: <b>{'включено' if rule.enabled else 'выключено'}</b>\n\n"
        f"Ответ:\n{html.escape(rule.response)[:2500]}",
        b,
    )


@router.callback_query(F.data.startswith("ar:toggle:"))
async def autoreply_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    rid = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(AutoReplyRule, rid)
        if rule and rule.account_id == account.id:
            rule.enabled = not rule.enabled
            await session.commit()
    await call.answer("Сохранено")
    await _show_autoreplies(call)


@router.callback_query(F.data.startswith("ar:remove:"))
async def autoreply_remove(call: CallbackQuery) -> None:
    account = await require_account(call)
    rid = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(AutoReplyRule, rid)
        if rule and rule.account_id == account.id:
            await session.delete(rule)
            await session.commit()
    await call.answer("Удалено")
    await _show_autoreplies(call)


async def _show_delivery(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        rules = list((await session.scalars(
            select(DeliveryRule).where(DeliveryRule.account_id == account.id).order_by(DeliveryRule.id)
        )).all())
        stock_counts = {}
        for rule in rules:
            stock_counts[rule.id] = int(await session.scalar(
                select(func.count(DeliveryStock.id)).where(
                    DeliveryStock.rule_id == rule.id, DeliveryStock.used_at.is_(None)
                )
            ) or 0)
    b = InlineKeyboardBuilder()
    for rule in rules:
        extra = f" · {stock_counts[rule.id]} шт." if rule.mode == "stock" else ""
        b.button(
            text=f"{'✅' if rule.enabled else '⏸'} {clip(rule.item_id, 22)}{extra}",
            callback_data=f"del:view:{rule.id}",
        )
    b.button(text="➕ Новое правило", callback_data="delivery:add")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    await edit(
        call,
        "⚡ <b>Автовыдача</b>\n"
        "Для склада можно пополнять остаток без пересоздания правила. "
        "ID <code>*</code> работает как правило по умолчанию.",
        b.as_markup(),
    )


@router.callback_query(F.data == "menu:delivery")
async def delivery_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _show_delivery(call)


@router.callback_query(F.data.startswith("del:view:"))
async def delivery_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    rid = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(DeliveryRule, rid)
        count = int(await session.scalar(select(func.count(DeliveryStock.id)).where(
            DeliveryStock.rule_id == rid, DeliveryStock.used_at.is_(None)
        )) or 0)
    if not rule or rule.account_id != account.id:
        return
    rows = [
        [InlineKeyboardButton(
            text="⏸ Выключить" if rule.enabled else "▶️ Включить",
            callback_data=f"del:toggle:{rid}",
        )]
    ]
    if rule.mode == "stock":
        rows.append([InlineKeyboardButton(text="➕ Пополнить склад", callback_data=f"del:stock:{rid}")])
    rows.extend([
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:remove:{rid}")],
        [InlineKeyboardButton(text="⬅️ Автовыдача", callback_data="menu:delivery")],
    ])
    await call.answer()
    await edit(
        call,
        f"⚡ <b>Автовыдача #{rid}</b>\n"
        f"Item ID: <code>{html.escape(rule.item_id)}</code>\n"
        f"Режим: <b>{html.escape(rule.mode)}</b>\n"
        f"Статус: <b>{'включено' if rule.enabled else 'выключено'}</b>\n"
        f"Остаток: <b>{count if rule.mode == 'stock' else '∞'}</b>\n\n"
        f"Шаблон: <code>{html.escape(rule.message_template)[:1400]}</code>",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("del:toggle:"))
async def delivery_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    rid = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(DeliveryRule, rid)
        if rule and rule.account_id == account.id:
            rule.enabled = not rule.enabled
            await session.commit()
    await call.answer("Сохранено")
    await _show_delivery(call)


@router.callback_query(F.data.startswith("del:stock:"))
async def delivery_stock_start(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    rid = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(DeliveryRule, rid)
    if not rule or rule.account_id != account.id or rule.mode != "stock":
        return
    await state.set_state(DeliveryStockAdd.content)
    await state.update_data(delivery_rule_id=rid)
    await call.answer()
    await edit(call, "Отправьте новые позиции: одна непустая строка = одна выдача.", back_menu("delivery"))


@router.message(DeliveryStockAdd.content)
async def delivery_stock_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = int(data["delivery_rule_id"])
    lines = [x.strip() for x in (message.text or "").splitlines() if x.strip()]
    if not lines:
        await message.answer("Склад пуст.")
        return
    account = await active_account(message.from_user.id)
    async with svc().db() as session:
        rule = await session.get(DeliveryRule, rid)
        if not rule or rule.account_id != account.id:
            await state.clear()
            return
        for line in lines:
            session.add(DeliveryStock(rule_id=rid, payload_encrypted=svc().cipher.encrypt(line)))
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Добавлено позиций: {len(lines)}", reply_markup=main_menu())


@router.callback_query(F.data.startswith("del:remove:"))
async def delivery_remove(call: CallbackQuery) -> None:
    account = await require_account(call)
    rid = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(DeliveryRule, rid)
        if rule and rule.account_id == account.id:
            await session.delete(rule)
            await session.commit()
    await call.answer("Удалено")
    await _show_delivery(call)


@router.callback_query(F.data == "menu:chats")
async def chats_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return
    await call.answer("Загружаю…")
    try:
        page = await (await svc().gateway.get_client(account)).get_chats(type=ChatTypes.PM, count=16)
        chats = list(getattr(page, "chats", []) or [])
    except Exception as exc:
        await edit(call, f"❌ <code>{html.escape(str(exc))[:1600]}</code>", back_menu())
        return
    b = InlineKeyboardBuilder()
    for chat in chats:
        unread = int(getattr(chat, "unread_messages_counter", 0) or 0)
        prefix = f"🔴 {unread} · " if unread else ""
        b.button(
            text=prefix + clip(_user_name(chat, str(account.playerok_user_id)), 30),
            callback_data=f"chat:open:{chat.id}",
        )
    b.button(text="🔄 Обновить", callback_data="menu:chats")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    await edit(call, f"💬 <b>Чаты</b>\nВсего: {getattr(page, 'total_count', len(chats))}", b.as_markup())


@router.callback_query(F.data.startswith("chat:open:"))
async def chat_open(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    chat_id = call.data.split(":", 2)[2]
    await call.answer("Открываю…")
    try:
        client = await svc().gateway.get_client(account)
        chat, page = await asyncio.gather(
            client.call("get_chat", chat_id),
            client.call("get_chat_messages", chat_id, count=24),
        )
        await client.call("mark_chat_as_read", chat_id)
    except Exception as exc:
        await edit(call, f"❌ <code>{html.escape(str(exc))[:1600]}</code>", back_menu("chats"))
        return
    messages = list(getattr(page, "messages", []) or [])
    page_info = getattr(page, "page_info", None)
    await state.update_data(
        history_chat_id=chat_id,
        history_cursor=getattr(page_info, "end_cursor", None),
        history_has_next=bool(getattr(page_info, "has_next_page", False)),
    )
    lines = [f"💬 <b>{html.escape(_user_name(chat, str(account.playerok_user_id)))}</b>"]
    for msg in reversed(messages):
        user = getattr(msg, "user", None)
        mine = str(getattr(user, "id", "")) == str(account.playerok_user_id)
        who = "Вы" if mine else str(getattr(user, "username", "Покупатель"))
        text = str(getattr(msg, "text", "") or "")
        images = list(getattr(msg, "images", []) or [])
        if images:
            text += f" [изображений: {len(images)}]"
        if not text:
            event = getattr(getattr(msg, "event", None), "name", None)
            text = f"[{event or 'системное сообщение'}]"
        lines.append(f"\n<b>{html.escape(who)}</b>: {html.escape(clip(text, 320))}")
    body = "\n".join(lines)
    if len(body) > 3900:
        body = body[-3900:]
        body = "…\n" + body
    rows = [
        [InlineKeyboardButton(text="✍️ Ответить", callback_data=f"chat:reply:{chat_id}")],
    ]
    if getattr(page_info, "has_next_page", False):
        rows.append([InlineKeyboardButton(text="⬅️ Более старые сообщения", callback_data="chat:more")])
    rows.extend([
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"chat:open:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Чаты", callback_data="menu:chats")],
    ])
    await edit(call, body, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "chat:more")
async def chat_more(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    data = await state.get_data()
    chat_id = data.get("history_chat_id")
    cursor = data.get("history_cursor")
    if not chat_id or not cursor:
        await call.answer("История устарела", show_alert=True)
        return
    await call.answer("Загружаю…")
    try:
        page = await (await svc().gateway.get_client(account)).call(
            "get_chat_messages", chat_id, count=24, after_cursor=cursor
        )
    except Exception as exc:
        await call.message.answer(f"❌ <code>{html.escape(str(exc))[:1500]}</code>", parse_mode="HTML")
        return
    messages = list(getattr(page, "messages", []) or [])
    page_info = getattr(page, "page_info", None)
    await state.update_data(
        history_cursor=getattr(page_info, "end_cursor", None),
        history_has_next=bool(getattr(page_info, "has_next_page", False)),
    )
    lines = ["💬 <b>Более старые сообщения</b>"]
    for msg in reversed(messages):
        user = getattr(msg, "user", None)
        mine = str(getattr(user, "id", "")) == str(account.playerok_user_id)
        who = "Вы" if mine else str(getattr(user, "username", "Покупатель"))
        text = str(getattr(msg, "text", "") or "")
        if getattr(msg, "images", None):
            text += f" [изображений: {len(msg.images)}]"
        if not text:
            text = f"[{getattr(getattr(msg, 'event', None), 'name', 'системное сообщение')}]"
        lines.append(f"\n<b>{html.escape(who)}</b>: {html.escape(clip(text, 320))}")
    body = "\n".join(lines)
    if len(body) > 3900:
        body = "…\n" + body[-3900:]
    rows = [[InlineKeyboardButton(text="✍️ Ответить", callback_data=f"chat:reply:{chat_id}")]]
    if getattr(page_info, "has_next_page", False):
        rows.append([InlineKeyboardButton(text="⬅️ Ещё старше", callback_data="chat:more")])
    rows.append([InlineKeyboardButton(text="↩️ К последним", callback_data=f"chat:open:{chat_id}")])
    await edit(call, body, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("chat:reply:"))
async def chat_reply_start(call: CallbackQuery, state: FSMContext) -> None:
    chat_id = call.data.split(":", 2)[2]
    await state.set_state(ChatReply.text)
    await state.update_data(reply_chat_id=chat_id)
    await call.answer()
    await edit(call, "✍️ Отправьте текст ответа:", back_menu("chats"))


@router.message(ChatReply.text)
async def chat_reply_value(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    data = await state.get_data()
    text = (message.text or "").strip()
    if not text:
        return
    try:
        await (await svc().gateway.get_client(account)).send_message(
            data["reply_chat_id"], text, mark_chat_as_read=True
        )
    except Exception as exc:
        await message.answer(f"❌ <code>{html.escape(str(exc))[:1500]}</code>", parse_mode="HTML")
        return
    await state.clear()
    await message.answer("✅ Ответ отправлен.", reply_markup=main_menu())


async def _show_plugins(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    manager = plugins()
    items = manager.ordered()
    b = InlineKeyboardBuilder()
    for i, plugin in enumerate(items):
        enabled, _ = await manager.resolved_state(account.id, plugin)
        b.button(text=f"{'✅' if enabled else '⏸'} {clip(plugin.name, 28)}", callback_data=f"plugin:view:{i}")
    b.button(text="🔄 Перезагрузить плагины", callback_data="plugins:reload")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    errors = f"\nОшибок загрузки: <b>{len(manager.load_errors)}</b>" if manager.load_errors else ""
    await edit(call, f"🧩 <b>Плагины</b>\nНайдено: <b>{len(items)}</b>{errors}", b.as_markup())


@router.callback_query(F.data == "menu:plugins")
async def plugins_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _show_plugins(call)


@router.callback_query(F.data == "plugins:reload")
async def plugins_reload(call: CallbackQuery) -> None:
    plugins().load()
    await call.answer("Перезагружено")
    await _show_plugins(call)


@router.callback_query(F.data.startswith("plugin:view:"))
async def plugin_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    idx = int(call.data.rsplit(":", 1)[-1])
    plugin = plugins().by_index(idx)
    if not plugin:
        await call.answer("Плагин не найден", show_alert=True)
        return
    enabled, _ = await plugins().resolved_state(account.id, plugin)
    rows = [
        [InlineKeyboardButton(
            text="⏸ Выключить" if enabled else "▶️ Включить",
            callback_data=f"plugin:toggle:{idx}",
        )],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"plugin:settings:{idx}")],
        [InlineKeyboardButton(text="⬅️ Плагины", callback_data="menu:plugins")],
    ]
    await call.answer()
    await edit(
        call,
        f"🧩 <b>{html.escape(plugin.name)}</b> v{html.escape(plugin.version)}\n"
        f"Автор: {html.escape(plugin.author)}\n"
        f"Статус: <b>{'включён' if enabled else 'выключен'}</b>\n\n"
        f"{html.escape(plugin.description or 'Без описания')}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("plugin:toggle:"))
async def plugin_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    idx = int(call.data.rsplit(":", 1)[-1])
    plugin = plugins().by_index(idx)
    if not plugin:
        return
    enabled, _ = await plugins().resolved_state(account.id, plugin)
    await plugins().set_enabled(account.id, plugin, not enabled)
    await call.answer("Сохранено")
    await _show_plugins(call)


@router.callback_query(F.data.startswith("plugin:settings:"))
async def plugin_settings(call: CallbackQuery) -> None:
    account = await require_account(call)
    idx = int(call.data.rsplit(":", 1)[-1])
    plugin = plugins().by_index(idx)
    if not plugin:
        return
    _enabled, config = await plugins().resolved_state(account.id, plugin)
    b = InlineKeyboardBuilder()
    for key, meta in plugin.settings.items():
        value = config.get(key)
        shown = "✅" if value is True else "❌" if value is False else clip(value, 16)
        b.button(text=f"{meta.get('label', key)}: {shown}", callback_data=f"pset:{idx}:{key}")
    b.button(text="⬅️ К плагину", callback_data=f"plugin:view:{idx}")
    b.adjust(1)
    await call.answer()
    await edit(
        call,
        f"⚙️ <b>Настройки: {html.escape(plugin.name)}</b>\n"
        + ("Выберите параметр." if plugin.settings else "У плагина нет настраиваемых параметров."),
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("pset:"))
async def plugin_setting_click(call: CallbackQuery, state: FSMContext) -> None:
    _prefix, idx_raw, key = call.data.split(":", 2)
    idx = int(idx_raw)
    account = await require_account(call)
    plugin = plugins().by_index(idx)
    if not plugin or key not in plugin.settings:
        return
    meta = plugin.settings[key]
    _enabled, config = await plugins().resolved_state(account.id, plugin)
    kind = meta.get("type", "str")
    if kind == "bool":
        await plugins().set_setting(account.id, plugin, key, not bool(config.get(key)))
        await call.answer("Сохранено")
        await _show_plugins(call)
        return
    if kind == "choice":
        choices = list(meta.get("choices") or [])
        b = InlineKeyboardBuilder()
        for ci, value in enumerate(choices):
            b.button(text=clip(value, 34), callback_data=f"pchoice:{idx}:{key}:{ci}")
        b.adjust(1)
        await call.answer()
        await edit(call, f"Выберите: <b>{html.escape(meta.get('label', key))}</b>", b.as_markup())
        return
    await state.set_state(PluginSettingEdit.value)
    await state.update_data(plugin_index=idx, plugin_setting_key=key)
    await call.answer()
    await edit(
        call,
        f"Введите новое значение для <b>{html.escape(meta.get('label', key))}</b>:",
        _settings_back(idx),
    )


@router.callback_query(F.data.startswith("pchoice:"))
async def plugin_choice(call: CallbackQuery) -> None:
    _p, idx_raw, key, ci_raw = call.data.split(":", 3)
    idx, ci = int(idx_raw), int(ci_raw)
    account = await require_account(call)
    plugin = plugins().by_index(idx)
    if not plugin:
        return
    choices = list(plugin.settings[key].get("choices") or [])
    if ci >= len(choices):
        return
    await plugins().set_setting(account.id, plugin, key, choices[ci])
    await call.answer("Сохранено")
    plugin = plugins().by_index(idx)
    if plugin:
        _enabled, config = await plugins().resolved_state(account.id, plugin)
        b = InlineKeyboardBuilder()
        for setting_key, meta in plugin.settings.items():
            value = config.get(setting_key)
            shown = "✅" if value is True else "❌" if value is False else clip(value, 16)
            b.button(
                text=f"{meta.get('label', setting_key)}: {shown}",
                callback_data=f"pset:{idx}:{setting_key}",
            )
        b.button(text="⬅️ К плагину", callback_data=f"plugin:view:{idx}")
        b.adjust(1)
        await edit(call, f"⚙️ <b>Настройки: {html.escape(plugin.name)}</b>", b.as_markup())


@router.message(PluginSettingEdit.value)
async def plugin_setting_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(data["plugin_index"])
    key = data["plugin_setting_key"]
    plugin = plugins().by_index(idx)
    account = await active_account(message.from_user.id)
    if not plugin or not account:
        await state.clear()
        return
    meta = plugin.settings[key]
    raw = (message.text or "").strip()
    if meta.get("type") == "int":
        try:
            value: Any = int(raw)
        except ValueError:
            await message.answer("Нужно целое число.")
            return
    else:
        value = raw
    await plugins().set_setting(account.id, plugin, key, value)
    await state.clear()
    await message.answer("✅ Настройка сохранена.", reply_markup=main_menu())
