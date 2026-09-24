from __future__ import annotations

import asyncio
import html
import uuid
from collections import defaultdict
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from playerokapi.enums import ChatTypes, ItemDealDirections

from .handlers import active_account, clip, edit, require_account, svc
from .keyboards import back_menu, main_menu
from .models import AutoReplyRule, DeliveryRule, DeliveryStock, PlayerokAccount
from .plugin_system import PluginManager
from .states import (
    AIPluginBuilderState,
    ChatReply,
    DeliveryStockAdd,
    ItemCatalog,
    ItemCreate,
    PluginSettingEdit,
    PluginUpload,
)
from .ai_plugin_builder import (
    AnthropicPluginBuilder,
    AIPluginBuilderError,
    inspect_generated_source,
    generated_filename,
    validate_api_base_url,
    validate_model_id,
    validate_plugin_request,
    DEFAULT_API_BASE_URL as AI_BUILDER_DEFAULT_BASE_URL,
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
        [InlineKeyboardButton(text="➕ Выставить товар (Поиск)", callback_data="catalog:search")],
        [InlineKeyboardButton(text="🎮 Ввести slug вручную", callback_data="items:create")],
        [InlineKeyboardButton(text="📚 Каталог: категория → игра", callback_data="catalog:categories")],
        [InlineKeyboardButton(text="📋 Мои товары", callback_data="items:list")],
        [InlineKeyboardButton(text="🧩 Шаблоны товаров", callback_data="templates:list")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main")],
    ])
    await edit(
        call,
        "📦 <b>Управление товарами</b>\n\n"
        "• <b>Выставить товар (Поиск):</b> быстрый поиск игры или приложения (например, <i>Claude</i>, <i>ChatGPT</i>, <i>Brawl Stars</i>).\n"
        "• <b>Ввести slug вручную:</b> для точного перехода к разделу по его коду (например: <code>claude</code>, <code>cgpt</code>, <code>brawl-stars</code>).\n"
        "• <b>Каталог:</b> древовидный просмотр рубрик.",
        markup,
    )


@router.callback_query(F.data == "catalog:start")
async def catalog_start(call: CallbackQuery) -> None:
    await call.answer()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Поиск игры или приложения", callback_data="catalog:search")],
        [InlineKeyboardButton(text="🎮 Ввести slug вручную", callback_data="items:create")],
        [InlineKeyboardButton(text="📚 Категория → игра", callback_data="catalog:categories")],
        [InlineKeyboardButton(text="⬅️ Товары", callback_data="menu:items")],
    ])
    await edit(call, "Как найти товарную категорию для выставления?", markup)


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


DEFAULT_CHATS_LIMIT = 24


@router.callback_query(F.data.startswith("menu:chats"))
async def chats_menu(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer("Загружаю…")

    # callback format: menu:chats:<filter>:<cursor_or_none>
    parts = call.data.split(":")
    chat_filter = parts[2] if len(parts) > 2 and parts[2] else "ALL"
    after_cursor = parts[3] if len(parts) > 3 and parts[3] != "_" else None

    # Determine ChatTypes enum
    target_type = None
    if chat_filter == "PM":
        target_type = ChatTypes.PM
    elif chat_filter == "NOTIFICATIONS":
        target_type = ChatTypes.NOTIFICATIONS
    elif chat_filter == "SUPPORT":
        target_type = ChatTypes.SUPPORT

    try:
        kwargs: dict[str, Any] = {"count": DEFAULT_CHATS_LIMIT}
        if target_type is not None:
            kwargs["type"] = target_type
        if after_cursor:
            kwargs["after_cursor"] = after_cursor
        page = await (await svc().gateway.get_client(account)).get_chats(**kwargs)
        chats = list(getattr(page, "chats", []) or [])
    except Exception as exc:
        await edit(call, f"❌ <code>{html.escape(str(exc))[:1600]}</code>", back_menu())
        return

    b = InlineKeyboardBuilder()

    # Filter selector tabs
    filter_labels = [
        ("Все", "ALL"),
        ("ЛС", "PM"),
        ("Уведомления", "NOTIFICATIONS"),
        ("Поддержка", "SUPPORT"),
    ]
    filter_row = []
    for flabel, fval in filter_labels:
        mark = "🔹 " if chat_filter == fval else ""
        filter_row.append(
            InlineKeyboardButton(text=f"{mark}{flabel}", callback_data=f"menu:chats:{fval}:_")
        )
    b.row(*filter_row)

    for chat in chats:
        unread = int(getattr(chat, "unread_messages_counter", 0) or 0)
        prefix = f"🔴 {unread} · " if unread else ""
        title = _user_name(chat, str(account.playerok_user_id))
        ctype = getattr(getattr(chat, "type", None), "name", "")
        type_prefix = ""
        if ctype == "NOTIFICATIONS":
            type_prefix = "🔔 "
        elif ctype == "SUPPORT":
            type_prefix = "🛟 "
        b.row(
            InlineKeyboardButton(
                text=prefix + type_prefix + clip(title, 28),
                callback_data=f"chat:open:{chat.id}",
            )
        )

    page_info = getattr(page, "page_info", None)
    has_next = bool(getattr(page_info, "has_next_page", False))
    end_cursor = getattr(page_info, "end_cursor", None)

    nav_row = []
    if has_next and end_cursor:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️ След. страница",
                callback_data=f"menu:chats:{chat_filter}:{end_cursor}",
            )
        )
    nav_row.append(
        InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data=f"menu:chats:{chat_filter}:{after_cursor or '_'}",
        )
    )
    b.row(*nav_row)
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))

    filter_desc = {
        "ALL": "Все диалоги",
        "PM": "Личные сообщения (ЛС)",
        "NOTIFICATIONS": "Уведомления Playerok",
        "SUPPORT": "Поддержка Playerok",
    }.get(chat_filter, chat_filter)

    total = getattr(page, "total_count", len(chats))
    await edit(
        call,
        f"💬 <b>Чаты Playerok</b>\n"
        f"Категория: <b>{filter_desc}</b>\n"
        f"Показано в списке: <b>{len(chats)}</b> (всего на аккаунте: <b>{total}</b>)\n\n"
        f"<i>Выберите диалог для просмотра сообщений, ответа текстом или отправки фото:</i>",
        b.as_markup(),
    )


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
        [
            InlineKeyboardButton(text="✍️ Ответить текстом", callback_data=f"chat:reply:{chat_id}"),
            InlineKeyboardButton(text="📷 Отправить фото", callback_data=f"chat:photo:{chat_id}"),
        ],
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
    rows = [
        [
            InlineKeyboardButton(text="✍️ Ответить", callback_data=f"chat:reply:{chat_id}"),
            InlineKeyboardButton(text="📷 Фото", callback_data=f"chat:photo:{chat_id}"),
        ]
    ]
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
    await edit(
        call,
        "✍️ <b>Ответ в чат</b>\n\nОтправьте текст сообщения (или нажмите «Назад»):",
        back_menu(f"chats"),
    )


@router.callback_query(F.data.startswith("chat:photo:"))
async def chat_photo_start(call: CallbackQuery, state: FSMContext) -> None:
    chat_id = call.data.split(":", 2)[2]
    await state.set_state(ChatReply.photo)
    await state.update_data(reply_chat_id=chat_id)
    await call.answer()
    await edit(
        call,
        "📷 <b>Отправка фото в чат</b>\n\n"
        "Пришлите фото (можно с текстовой подписью) для отправки в диалог Playerok:",
        back_menu("chats"),
    )


@router.message(ChatReply.text)
async def chat_reply_value(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await message.answer("❌ Аккаунт не найден.")
        return
    data = await state.get_data()
    chat_id = data.get("reply_chat_id")
    if not chat_id:
        await message.answer("❌ Ошибка: чат не выбран.", reply_markup=main_menu())
        await state.clear()
        return

    # If user sent photo while in text reply mode, handle it seamlessly
    if message.photo:
        await _handle_chat_photo_send(message, state, account, chat_id)
        return

    text = (message.text or "").strip()
    if not text:
        return
    try:
        await (await svc().gateway.get_client(account)).send_message(
            chat_id, text=text, mark_chat_as_read=True
        )
    except Exception as exc:
        await message.answer(f"❌ <code>{html.escape(str(exc))[:1500]}</code>", parse_mode="HTML")
        return
    await state.clear()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Вернуться в чат", callback_data=f"chat:open:{chat_id}")],
        [InlineKeyboardButton(text="📋 К списку чатов", callback_data="menu:chats")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main")],
    ])
    await message.answer("✅ Ответ отправлен в Playerok.", reply_markup=markup)


@router.message(ChatReply.photo, F.photo)
async def chat_reply_photo(message: Message, state: FSMContext, bot: Any) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await message.answer("❌ Аккаунт не найден.")
        return
    data = await state.get_data()
    chat_id = data.get("reply_chat_id")
    if not chat_id:
        await message.answer("❌ Ошибка: чат не выбран.", reply_markup=main_menu())
        await state.clear()
        return
    await _handle_chat_photo_send(message, state, account, chat_id)


async def _handle_chat_photo_send(message: Message, state: FSMContext, account: Any, chat_id: str) -> None:
    caption = (message.caption or "").strip() or None
    wait_msg = await message.answer("⏳ Загружаю и отправляю изображение в Playerok…")
    try:
        from io import BytesIO
        buffer = BytesIO()
        await message.bot.download(message.photo[-1], destination=buffer)
        image_bytes = buffer.getvalue()

        client = await svc().gateway.get_client(account)
        await client.send_message(
            chat_id=chat_id,
            text=caption,
            images=[image_bytes],
            mark_chat_as_read=True,
        )
    except Exception as exc:
        await wait_msg.edit_text(f"❌ <code>{html.escape(str(exc))[:1500]}</code>", parse_mode="HTML")
        return

    await state.clear()
    await wait_msg.delete()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Вернуться в чат", callback_data=f"chat:open:{chat_id}")],
        [InlineKeyboardButton(text="📋 К списку чатов", callback_data="menu:chats")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main")],
    ])
    await message.answer("✅ Фото успешно отправлено в чат Playerok.", reply_markup=markup)


# --- Deals & Orders UI ---

@router.callback_query(F.data == "menu:deals")
async def show_deals_menu(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer("Загружаю сделки…")
    try:
        client = await svc().gateway.get_client(account)
        page = await asyncio.to_thread(client.get_deals, direction=ItemDealDirections.OUT, count=24)
        deals = list(getattr(page, "deals", []) or [])
    except Exception as exc:
        await edit(call, f"❌ Ошибка загрузки сделок: <code>{html.escape(str(exc))[:600]}</code>", back_menu("main"))
        return

    b = InlineKeyboardBuilder()
    for deal in deals:
        status_obj = getattr(deal, "status", None)
        status_name = getattr(status_obj, "name", "—")
        status_icon = "⏳"
        if status_name == "SENT":
            status_icon = "✅"
        elif status_name in {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}:
            status_icon = "🤝"
        elif status_name == "ROLLED_BACK":
            status_icon = "↩️"

        item = getattr(deal, "item", None)
        item_title = getattr(item, "name", None) or "Товар"
        price = getattr(item, "price", None)
        price_str = f" · {price}₽" if price is not None else ""

        btn_text = f"{status_icon} #{str(deal.id)[-6:]} · {clip(item_title, 20)}{price_str}"
        b.button(text=btn_text, callback_data=f"deal:view:{deal.id}")

    b.button(text="🔄 Обновить", callback_data="menu:deals")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)

    await edit(
        call,
        f"📦 <b>Сделки и заказы Playerok</b>\n"
        f"Аккаунт: <b>{html.escape(account.username)}</b>\n"
        f"Найдено: <b>{len(deals)}</b> сделок\n\n"
        "<i>Нажмите на сделку для просмотра деталей или изменения статуса:</i>",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("deal:view:"))
async def deal_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    deal_id = call.data.split(":", 2)[2]
    await call.answer("Загружаю…")
    try:
        client = await svc().gateway.get_client(account)
        deal = await asyncio.to_thread(client.get_deal, deal_id)
    except Exception as exc:
        await edit(call, f"❌ Сделка недоступна: <code>{html.escape(str(exc))[:600]}</code>", back_menu("deals"))
        return

    item = getattr(deal, "item", None)
    buyer = getattr(deal, "user", None)
    chat = getattr(deal, "chat", None)
    chat_id = getattr(chat, "id", None)
    status_obj = getattr(deal, "status", None)
    status_name = getattr(status_obj, "name", "—")

    status_ru = {
        "PAID": "Оплачен, ожидает выполнения",
        "PENDING": "В ожидании отправки",
        "SENT": "Выполнен продавцом (ожидает подтверждения)",
        "CONFIRMED": "Подтверждён покупателем",
        "CONFIRMED_AUTOMATICALLY": "Подтверждён автоматически",
        "ROLLED_BACK": "Возврат средств",
    }.get(status_name, status_name)

    rows = []
    if status_name in {"PENDING", "PAID"}:
        rows.append([InlineKeyboardButton(text="✅ Отметить выполненным", callback_data=f"deal:sent_ask:{deal_id}")])
    if chat_id:
        rows.append([InlineKeyboardButton(text="💬 Открыть чат", callback_data=f"chat:open:{chat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку сделок", callback_data="menu:deals")])

    price = getattr(item, "price", 0)
    comment = getattr(deal, "comment_from_buyer", None) or "—"
    text = (
        "📦 <b>Сделка Playerok</b>\n\n"
        f"🆔 ID: <code>{html.escape(str(deal.id))}</code>\n"
        f"📌 Статус: <b>{html.escape(status_ru)}</b>\n"
        f"👤 Покупатель: <b>{html.escape(str(getattr(buyer, 'username', '—')))}</b>\n"
        f"🏷 Товар: <b>{html.escape(str(getattr(item, 'name', '—')))}</b>\n"
        f"💰 Сумма: <b>{price} ₽</b>\n"
        f"💬 Комментарий покупателя: <pre>{html.escape(str(comment)[:800])}</pre>"
    )
    await edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("deal:sent_ask:"))
async def deal_sent_ask(call: CallbackQuery) -> None:
    deal_id = call.data.split(":", 2)[2]
    await call.answer()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, подтвердить выполнение", callback_data=f"deal:sent_do:{deal_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"deal:view:{deal_id}")],
    ])
    await edit(
        call,
        f"❓ <b>Отметить сделку #{deal_id[-6:]} выполненной?</b>\n\n"
        "Статус заказа на Playerok изменится на «Отправлен», и покупатель получит запрос на подтверждение и отзыв.",
        markup,
    )


@router.callback_query(F.data.startswith("deal:sent_do:"))
async def deal_sent_do(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    deal_id = call.data.split(":", 2)[2]
    await call.answer("Отправляю…")
    try:
        from playerokapi.enums import ItemDealStatuses
        client = await svc().gateway.get_client(account)
        await asyncio.to_thread(client.update_deal, deal_id, ItemDealStatuses.SENT)
    except Exception as exc:
        await call.answer(f"❌ Не удалось: {str(exc)[:120]}", show_alert=True)
        return

    await call.answer("✅ Сделка отмечена выполненной", show_alert=True)
    # Refresh deal view
    try:
        deal = await asyncio.to_thread(client.get_deal, deal_id)
        item = getattr(deal, "item", None)
        buyer = getattr(deal, "user", None)
        chat = getattr(deal, "chat", None)
        chat_id = getattr(chat, "id", None)
        rows = []
        if chat_id:
            rows.append([InlineKeyboardButton(text="💬 Открыть чат", callback_data=f"chat:open:{chat_id}")])
        rows.append([InlineKeyboardButton(text="⬅️ К списку сделок", callback_data="menu:deals")])
        await edit(
            call,
            "✅ <b>Заказ успешно выполнен продавцом!</b>\n\n"
            f"🆔 ID: <code>{html.escape(str(deal.id))}</code>\n"
            f"📌 Статус: <b>Выполнен продавцом (ожидает подтверждения)</b>\n"
            f"👤 Покупатель: <b>{html.escape(str(getattr(buyer, 'username', '—')))}</b>\n"
            f"🏷 Товар: <b>{html.escape(str(getattr(item, 'name', '—')))}</b>\n\n"
            "<i>Ожидается подтверждение получения и оставление отзыва покупателем.</i>",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception:
        await show_deals_menu(call)


# --- Plugins UI ---

async def _show_my_plugins(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    manager = plugins()
    items = manager.ordered()
    b = InlineKeyboardBuilder()
    for i, plugin in enumerate(items):
        enabled, _ = await manager.resolved_state(account.id, plugin)
        b.button(text=f"{'✅' if enabled else '⏸'} {clip(plugin.name, 26)}", callback_data=f"plugin:view:{i}")
    b.button(text="🧭 Каталог плагинов", callback_data="plugins:catalog:0")
    b.button(text="✨ Создать плагин (AI)", callback_data="plugins:ai_builder")
    b.button(text="➕ Загрузить плагин (.py)", callback_data="plugins:upload_warning")
    b.button(text="📚 Документация", callback_data="plugins:docs")
    b.button(text="🔄 Перезагрузить плагины", callback_data="plugins:reload")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    errors = f"\n⚠️ Ошибок загрузки: <b>{len(manager.load_errors)}</b>" if manager.load_errors else ""
    await edit(
        call,
        f"🧩 <b>Мои плагины</b>\nУстановлено: <b>{len(items)}</b>{errors}\n\n"
        "Нажмите на плагин для просмотра карточки, настройки параметров или включения/выключения:",
        b.as_markup(),
    )


async def _show_plugins(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    manager = plugins()
    items = manager.ordered()
    b = InlineKeyboardBuilder()
    b.button(text="🧭 Каталог плагинов", callback_data="plugins:catalog:0")
    b.button(text=f"🧩 Мои плагины ({len(items)})", callback_data="plugins:mine")
    b.button(text="✨ Создать плагин (AI)", callback_data="plugins:ai_builder")
    b.button(text="➕ Загрузить плагин", callback_data="plugins:upload_warning")
    b.button(text="📚 Документация", callback_data="plugins:docs")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    errors = f"\nОшибок загрузки: <b>{len(manager.load_errors)}</b>" if manager.load_errors else ""
    await edit(
        call,
        "🧩 <b>Плагины Playerok</b>\n"
        f"Установлено: <b>{len(items)}</b>{errors}\n\n"
        "Каталог содержит готовые официальные расширения и скрипты. "
        "Вы можете создать свой плагин с помощью ИИ или загрузить готовый Python-скрипт.",
        b.as_markup(),
    )


@router.callback_query(F.data == "menu:plugins")
async def plugins_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _show_plugins(call)


@router.callback_query(F.data == "plugins:mine")
async def plugins_mine(call: CallbackQuery) -> None:
    await call.answer()
    await _show_my_plugins(call)


@router.callback_query(F.data.startswith("plugins:catalog"))
async def plugins_catalog(call: CallbackQuery) -> None:
    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    manager = plugins()
    catalog = manager.catalog_plugins()
    installed = {plugin.id for plugin in manager.ordered()}

    page_size = 5
    total_pages = max(1, (len(catalog) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    current_items = catalog[page * page_size : (page + 1) * page_size]

    b = InlineKeyboardBuilder()
    for plugin in current_items:
        status_icon = "✅ " if plugin.id in installed else "📥 "
        b.button(
            text=f"{status_icon}{clip(plugin.name, 26)}",
            callback_data=f"plugincatalog:view:{plugin.id}:{page}",
        )
    b.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"plugins:catalog:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"Стр. {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"plugins:catalog:{page + 1}"))
    if nav:
        b.row(*nav)

    b.row(
        InlineKeyboardButton(text=f"🧩 Мои плагины ({len(installed)})", callback_data="plugins:mine"),
        InlineKeyboardButton(text="✨ Создать через ИИ", callback_data="plugins:ai_builder"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Плагины", callback_data="menu:plugins"))

    text = (
        "🧭 <b>Каталог готовых плагинов Playerok</b>\n\n"
        f"Страница <b>{page + 1}</b> из <b>{total_pages}</b> (всего: <b>{len(catalog)}</b>):\n\n"
        + ("\n\n".join(
            f"{'✅' if p.id in installed else '▫️'} <b>{html.escape(p.name)}</b> v{html.escape(p.version)}\n"
            f"   <i>{html.escape(p.description)}</i>"
            for p in current_items
        ) or "Каталог пока пуст.")
    )
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("plugincatalog:view:"))
async def plugin_catalog_view(call: CallbackQuery) -> None:
    parts = call.data.split(":")
    plugin_id = parts[2]
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    plugin = next((item for item in plugins().catalog_plugins() if item.id == plugin_id), None)
    if not plugin:
        await call.answer("Плагин не найден", show_alert=True)
        return
    installed = plugin.id in plugins().plugins
    rows = []
    if not installed:
        rows.append([InlineKeyboardButton(text="📥 Установить плагин", callback_data=f"plugincatalog:install:{plugin.id}:{page}")])
    else:
        installed_plugin_idx = next((i for i, p in enumerate(plugins().ordered()) if p.id == plugin.id), None)
        if installed_plugin_idx is not None:
            rows.append([InlineKeyboardButton(text="⚙️ Открыть настройки", callback_data=f"plugin:view:{installed_plugin_idx}")])
        rows.append([InlineKeyboardButton(text="🗑 Удалить из установленных", callback_data=f"plugin:delete:{plugin.id}")])
    rows.append([
        InlineKeyboardButton(text="💾 Скачать исходник (.py)", callback_data=f"plugincatalog:source:{plugin.id}"),
        InlineKeyboardButton(text="⬅️ В каталог", callback_data=f"plugins:catalog:{page}"),
    ])
    await call.answer()
    status_text = "✅ <b>Установлен</b>" if installed else "▫️ <b>Не установлен</b>"
    await edit(
        call,
        f"🧭 <b>{html.escape(plugin.name)}</b> v{html.escape(plugin.version)}\n"
        f"Автор: <b>{html.escape(plugin.author)}</b>\n"
        f"Статус: {status_text}\n\n"
        f"<b>Описание:</b>\n{html.escape(plugin.description)}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("plugincatalog:source:"))
async def plugin_catalog_source(call: CallbackQuery) -> None:
    plugin_id = call.data.split(":", 2)[2]
    source_path = next((path for path in plugins().catalog_dir.glob("*.py") if path.stem == plugin_id), None)
    if not source_path:
        await call.answer("Файл не найден", show_alert=True)
        return
    await call.answer("Отправляю файл…")
    await call.message.answer_document(
        BufferedInputFile(source_path.read_bytes(), filename=source_path.name),
        caption=f"📄 Исходный код плагина <b>{html.escape(source_path.stem)}</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("plugincatalog:install:"))
async def plugin_catalog_install(call: CallbackQuery) -> None:
    parts = call.data.split(":")
    plugin_id = parts[2]
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    try:
        plugin = plugins().install_catalog(plugin_id)
    except Exception as exc:
        await call.answer(f"Ошибка: {str(exc)[:180]}", show_alert=True)
        return
    await call.answer(f"✅ Установлен: {plugin.name}", show_alert=True)
    await _show_my_plugins(call)


@router.callback_query(F.data.startswith("plugin:delete:"))
async def plugin_delete(call: CallbackQuery) -> None:
    plugin_id = call.data.split(":", 2)[2]
    try:
        plugins().uninstall(plugin_id)
    except Exception as exc:
        await call.answer(f"Ошибка удаления: {str(exc)[:180]}", show_alert=True)
        return
    await call.answer("🗑 Плагин удалён", show_alert=True)
    await _show_my_plugins(call)


# --- Documentation UI ---

@router.callback_query(F.data == "plugins:docs")
async def plugins_docs_menu(call: CallbackQuery) -> None:
    await call.answer()
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Быстрый старт", callback_data="plugins:doc:quickstart")
    b.button(text="📋 Структура PLUGIN_META", callback_data="plugins:doc:meta")
    b.button(text="⚡ События и хуки", callback_data="plugins:doc:hooks")
    b.button(text="⚙️ Типы настроек", callback_data="plugins:doc:settings")
    b.button(text="🛡 Безопасность и правила", callback_data="plugins:doc:security")
    b.button(text="💾 Скачать PLUGINS_FOR_AI.md", callback_data="plugins:docs_download")
    b.button(text="⬅️ Плагины", callback_data="menu:plugins")
    b.adjust(1)
    await edit(
        call,
        "📚 <b>Документация Playerok Plugin SDK</b>\n\n"
        "Плагины позволяют расширять возможности бота: создавать автоответы, интеграции с внешними сервисами, "
        "дополнительные уведомления, калькуляторы и автовыдачу.\n\n"
        "Выберите интересующий раздел или скачайте полный MD-файл для отправки в ИИ:",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("plugins:doc:"))
async def plugins_doc_section(call: CallbackQuery) -> None:
    sec = call.data.split(":", 2)[2]
    await call.answer()
    sections = {
        "quickstart": (
            "🚀 <b>Быстрый старт</b>\n\n"
            "Плагин — это один файл на Python (.py) размером до 256 КБ.\n"
            "1. Создайте файл плагина с объявлением <code>PLUGIN_META</code>.\n"
            "2. Реализуйте нужные функции (<code>on_message</code>, <code>on_deal</code> и др.).\n"
            "3. Загрузите файл через <b>➕ Загрузить плагин</b> или используйте <b>✨ Создать плагин (AI)</b>.\n"
            "4. Включите плагин в <b>🧩 Мои плагины</b>."
        ),
        "meta": (
            "📋 <b>Структура PLUGIN_META</b>\n\n"
            "Каждый плагин обязан содержать словарь:\n"
            "<pre>PLUGIN_META = {\n"
            '    "id": "my_plugin",\n'
            '    "name": "Название",\n'
            '    "version": "1.0.0",\n'
            '    "author": "Автор",\n'
            '    "description": "Описание",\n'
            '    "settings": {...}\n'
            "}</pre>\n"
            "ID должен состоять из латинских букв, цифр, символов <code>_</code>, <code>-</code>, <code>.</code> (до 32 символов)."
        ),
        "hooks": (
            "⚡ <b>События и хуки</b>\n\n"
            "Функции вызываются автоматически при событиях Playerok:\n"
            "• <code>async def on_message(ctx, chat, message):</code> — новое сообщение в чате\n"
            "• <code>async def on_deal(ctx, deal):</code> — новый заказ/сделка\n"
            "• <code>async def on_deal_changed(ctx, deal, prev_status):</code> — изменение статуса сделки\n"
            "• <code>async def on_review(ctx, review):</code> — новый отзыв покупателя\n"
            "• <code>async def on_schedule(ctx):</code> — периодический запуск\n"
            "• <code>async def on_action(ctx, action, payload):</code> — ручные действия"
        ),
        "settings": (
            "⚙️ <b>Типы настроек в PLUGIN_META</b>\n\n"
            "Поддерживаемые типы полей:\n"
            "• <code>bool</code> — переключатель (вкл/выкл)\n"
            "• <code>str</code> — строка/текст\n"
            "• <code>int</code> — целое число\n"
            "• <code>choice</code> — выпадающий список (выбор из вариантов)"
        ),
        "security": (
            "🛡 <b>Безопасность</b>\n\n"
            "• Запрещены модули <code>subprocess</code> и <code>ctypes</code>.\n"
            "• Запрещены функции <code>eval</code>, <code>exec</code>, <code>compile</code>.\n"
            "• Никогда не читайте и не логируйте переменные окружения и токены бота.\n"
            "• Все ключи внешних API настраиваются через <code>PLUGIN_META['settings']</code>."
        ),
    }
    content = sections.get(sec, "Раздел не найден")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К списку разделов", callback_data="plugins:docs")],
        [InlineKeyboardButton(text="⬅️ Меню плагинов", callback_data="menu:plugins")],
    ])
    await edit(call, content, markup)


@router.callback_query(F.data == "plugins:docs_download")
async def plugins_docs_download(call: CallbackQuery) -> None:
    await call.answer("Отправляю документ…")
    await call.message.answer_document(
        BufferedInputFile(plugins().documentation().encode("utf-8"), filename="PLAYEROK_PLUGINS_FOR_AI.md"),
        caption="📘 <b>Документация Playerok Plugin SDK</b>\nОтправьте этот файл в Claude/ChatGPT вместе с описанием нужного плагина.",
        parse_mode="HTML",
    )


# --- Upload with Warning ---

@router.callback_query(F.data == "plugins:upload_warning")
async def plugins_upload_warning(call: CallbackQuery) -> None:
    await call.answer()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Я понимаю риски, продолжить", callback_data="plugins:upload_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:plugins")],
    ])
    await edit(
        call,
        "⚠️ <b>Предупреждение о безопасности</b>\n\n"
        "Плагины выполняются с правами процесса бота. Сторонний непроверенный код может получить доступ к вашему аккаунту.\n\n"
        "Устанавливайте только собственные плагины либо плагины из официального каталога бота. Вы уверены, что хотите продолжить?",
        markup,
    )


@router.callback_query(F.data == "plugins:upload_confirm")
async def plugins_upload_confirm(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginUpload.file)
    await call.answer()
    await edit(call, "📤 <b>Загрузка плагина</b>\n\nПришлите файл плагина с расширением <code>.py</code> документом в чат.", back_menu("plugins"))


# --- AI Plugin Builder ---

@router.callback_query(F.data == "plugins:ai_builder")
async def ai_builder_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    cfg = settings_for_ai(account)
    configured = bool(cfg.get("api_key") and cfg.get("model_id"))
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Описать и создать плагин", callback_data="ai_builder:create")],
        [InlineKeyboardButton(text="⚙️ Настройки API (Ключ / Модель)", callback_data="ai_builder:settings")],
        [InlineKeyboardButton(text="⬅️ Плагины", callback_data="menu:plugins")],
    ])
    await edit(
        call,
        "✨ <b>AI-конструктор плагинов Playerok</b>\n\n"
        "Опишите нужный плагин простыми словами на русском языке. ИИ сгенерирует исходный код строго по документации SDK, "
        "проверит синтаксис и правила безопасности, после чего плагин автоматически появится в ваших установленных плагинах.\n\n"
        f"API: <b>{'✅ Настроено' if configured else '❌ Не настроено'}</b>\n"
        f"Модель: <code>{html.escape(cfg.get('model_id', 'claude-3-5-sonnet-20241022'))}</code>",
        markup,
    )


def settings_for_ai(account: PlayerokAccount) -> dict[str, Any]:
    cfg = dict(account.settings or {})
    return cfg.get("ai_builder", {
        "api_base_url": AI_BUILDER_DEFAULT_BASE_URL,
        "api_key": "",
        "model_id": "claude-3-5-sonnet-20241022",
    })


@router.callback_query(F.data == "ai_builder:settings")
async def ai_builder_settings_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    cfg = settings_for_ai(account)
    key_mask = (cfg.get("api_key")[:6] + "..." + cfg.get("api_key")[-4:]) if len(cfg.get("api_key", "")) > 10 else ("задан" if cfg.get("api_key") else "не задан")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Изменить API Key", callback_data="ai_builder:set:key")],
        [InlineKeyboardButton(text="🧠 Изменить ID модели", callback_data="ai_builder:set:model")],
        [InlineKeyboardButton(text="🌐 Изменить Base URL", callback_data="ai_builder:set:base")],
        [InlineKeyboardButton(text="⬅️ Назад в AI-конструктор", callback_data="plugins:ai_builder")],
    ])
    await edit(
        call,
        "⚙️ <b>Настройки AI-конструктора</b>\n\n"
        f"Base URL: <code>{html.escape(cfg.get('api_base_url', AI_BUILDER_DEFAULT_BASE_URL))}</code>\n"
        f"API Key: <code>{html.escape(key_mask)}</code>\n"
        f"Модель: <code>{html.escape(cfg.get('model_id', 'claude-3-5-sonnet-20241022'))}</code>\n\n"
        "<i>Поддерживается Anthropic Messages API, а также любые совместимые прокси (OpenRouter, Proxies и т.д.).</i>",
        markup,
    )


@router.callback_query(F.data.startswith("ai_builder:set:"))
async def ai_builder_set_field(call: CallbackQuery, state: FSMContext) -> None:
    field = call.data.split(":", 2)[2]
    await call.answer()
    if field == "key":
        await state.set_state(AIPluginBuilderState.api_token)
        await edit(call, "🔑 <b>Введите API-токен (API Key)</b>:\n\nСообщение с токеном будет удалено для безопасности. Для отмены: /cancel", back_menu("plugins"))
    elif field == "model":
        await state.set_state(AIPluginBuilderState.model_id)
        await edit(call, "🧠 <b>Введите точный ID модели</b> (например, <code>claude-3-5-sonnet-20241022</code>):\n\nДля отмены: /cancel", back_menu("plugins"))
    elif field == "base":
        await state.set_state(AIPluginBuilderState.api_base_url)
        await edit(call, f"🌐 <b>Введите HTTPS Base URL</b> (по умолчанию <code>{AI_BUILDER_DEFAULT_BASE_URL}</code>):\n\nДля отмены: /cancel", back_menu("plugins"))


@router.message(AIPluginBuilderState.api_token, F.text)
async def ai_builder_save_key(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if len(token) < 5:
        await message.answer("Токен слишком короткий.")
        return
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        if "ai_builder" not in s:
            s["ai_builder"] = {}
        s["ai_builder"]["api_key"] = token
        row.settings = copy.deepcopy(s)
        await session.commit()
        account.settings = s
    await state.clear()
    await message.answer("✅ API-токен успешно сохранён.", reply_markup=main_menu())


@router.message(AIPluginBuilderState.model_id, F.text)
async def ai_builder_save_model(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    model = (message.text or "").strip()
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        if "ai_builder" not in s:
            s["ai_builder"] = {}
        s["ai_builder"]["model_id"] = model
        row.settings = copy.deepcopy(s)
        await session.commit()
        account.settings = s
    await state.clear()
    await message.answer(f"✅ ID модели установлен: <code>{html.escape(model)}</code>", parse_mode="HTML", reply_markup=main_menu())


@router.message(AIPluginBuilderState.api_base_url, F.text)
async def ai_builder_save_base(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    url = (message.text or "").strip()
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        if "ai_builder" not in s:
            s["ai_builder"] = {}
        s["ai_builder"]["api_base_url"] = url
        row.settings = copy.deepcopy(s)
        await session.commit()
        account.settings = s
    await state.clear()
    await message.answer(f"✅ Base URL установлен: <code>{html.escape(url)}</code>", parse_mode="HTML", reply_markup=main_menu())


@router.callback_query(F.data == "ai_builder:create")
async def ai_builder_create_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    cfg = settings_for_ai(account)
    if not cfg.get("api_key"):
        await call.answer("Сначала укажите API Key в настройках!", show_alert=True)
        return
    await call.answer()
    await state.set_state(AIPluginBuilderState.request)
    await edit(
        call,
        "✨ <b>Создание нового плагина через ИИ</b>\n\n"
        "Подробно опишите, что должен делать плагин. Например:\n"
        "<i>«Создай плагин автоответа на частые вопросы покупателей. Если покупатель спрашивает про гарантию или привязку, отвечать подробной инструкцией. Добавь настройки для текста ответов и включения автоответа.»</i>\n\n"
        "Отправьте ваш запрос текстовым сообщением (для отмены: /cancel):",
        back_menu("plugins"),
    )


@router.message(AIPluginBuilderState.request, F.text)
async def ai_builder_handle_request(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    req_text = (message.text or "").strip()
    if len(req_text) < 15:
        await message.answer("Пожалуйста, опишите задачу подробнее (минимум 15 символов).")
        return
    await state.clear()
    cfg = settings_for_ai(account)
    api_key = cfg.get("api_key")
    base_url = cfg.get("api_base_url") or AI_BUILDER_DEFAULT_BASE_URL
    model_id = cfg.get("model_id") or "claude-3-5-sonnet-20241022"

    status_msg = await message.answer("🤖 <b>ИИ создаёт плагин…</b>\n1/2 · Генерация кода по SDK Playerok")
    try:
        builder = AnthropicPluginBuilder(api_key, base_url, model_id)
        docs = plugins().documentation()
        draft = await builder.create_draft(req_text, docs)
        try:
            await status_msg.edit_text("🤖 <b>ИИ проверяет плагин…</b>\n2/2 · Проверка синтаксиса и контракта")
        except Exception:
            pass
        reviewed = await builder.review_draft(req_text, docs, draft)
        meta = inspect_generated_source(reviewed.source)
        filename = generated_filename(meta["name"], meta["id"])
        plugin = plugins().install(filename, reviewed.source.encode("utf-8"))
        await plugins().set_enabled(account.id, plugin, True)

        await status_msg.edit_text(
            f"✅ <b>Плагин успешно создан и установлен!</b>\n\n"
            f"🧩 <b>{html.escape(plugin.name)}</b> v{html.escape(plugin.version)}\n"
            f"ID: <code>{html.escape(plugin.id)}</code>\n\n"
            f"📝 <b>Что сделано:</b>\n{html.escape(reviewed.summary[:1500])}",
            reply_markup=main_menu(),
        )
    except Exception as exc:
        await status_msg.edit_text(
            f"❌ <b>Не удалось создать плагин</b>:\n\n<code>{html.escape(str(exc))[:1500]}</code>",
            reply_markup=main_menu(),
        )


@router.callback_query(F.data == "plugins:reload")
async def plugins_reload(call: CallbackQuery) -> None:
    plugins().load()
    await call.answer("Перезагружено")
    await _show_plugins(call)


@router.callback_query(F.data == "plugins:upload")
async def plugins_upload_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginUpload.file)
    await call.answer()
    await edit(call, "📤 Пришлите файл плагина `.py` документом.", back_menu("plugins"))


@router.message(PluginUpload.file, F.document)
async def plugins_upload_file(message: Message, state: FSMContext, bot: Any) -> None:
    document = message.document
    if not document.file_name or not document.file_name.endswith(".py"):
        await message.answer("Нужен файл с расширением .py")
        return
    if document.file_size and document.file_size > 256 * 1024:
        await message.answer("Файл слишком большой (максимум 256 КБ).")
        return
    buffer = await bot.download(document)
    try:
        plugin = plugins().install(document.file_name, buffer.read())
    except Exception as exc:
        await message.answer(f"❌ Не удалось загрузить плагин: <code>{html.escape(str(exc))[:1200]}</code>", parse_mode="HTML")
        return
    await state.clear()
    await message.answer(
        f"✅ Плагин загружен: <b>{html.escape(plugin.name)}</b> v{html.escape(plugin.version)}",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


@router.message(PluginUpload.file)
async def plugins_upload_wrong_type(message: Message) -> None:
    await message.answer("Пришлите плагин именно документом, одним файлом .py.")


@router.callback_query(F.data.startswith("plugin_action:"))
async def plugin_action(call: CallbackQuery) -> None:
    parts = call.data.split(":", 2)
    action = parts[1]
    extra = parts[2] if len(parts) > 2 else ""
    account = await require_account(call)
    if not account:
        return
    try:
        client = await svc().gateway.get_client(account)
        payload = {"deal_id": extra} if extra and not extra.startswith("idx_") else {"param": extra}
        if extra.startswith("idx_"):
            payload["plugin_index"] = extra.replace("idx_", "")
        await plugins().dispatch_action(
            account,
            client,
            call.bot,
            action,
            payload,
        )
    except Exception as exc:
        await call.answer(f"Ошибка: {str(exc)[:180]}", show_alert=True)
        return
    await call.answer("Запрос обработан")


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
    ]
    if hasattr(plugin.module, "on_action"):
        rows.append([InlineKeyboardButton(text="🔌 Проверить соединение / Баланс", callback_data=f"plugin_action:test_connection:idx_{idx}")])
    rows.extend([
        [InlineKeyboardButton(text="🗑 Удалить плагин", callback_data=f"plugin:delete:{plugin.id}")],
        [InlineKeyboardButton(text="⬅️ Мои плагины", callback_data="plugins:mine")],
    ])
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
