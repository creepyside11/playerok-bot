from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select

from .handlers import active_account, clip, edit, require_account, svc
from .kosell_manager import (
    DEFAULT_KOSELL_DELIVERY_TEMPLATE,
    KosellAccountConfig,
    KosellClient,
    KosellDeal,
    KosellLotRule,
)
from .models import PlayerokAccount
from .plugin_system import PluginState
from .states import KosellState

logger = logging.getLogger("kosell_ui")
router = Router(name="kosell_ui")


async def _get_kosell_client(account: PlayerokAccount) -> tuple[KosellClient | None, KosellAccountConfig | None]:
    async with svc().db() as session:
        cfg = await session.scalar(
            select(KosellAccountConfig).where(KosellAccountConfig.account_id == account.id)
        )
    if not cfg or not cfg.api_key_encrypted:
        return None, cfg

    cipher = svc().cipher
    try:
        api_key = cipher.decrypt(cfg.api_key_encrypted) if cipher else cfg.api_key_encrypted
    except Exception:
        api_key = cfg.api_key_encrypted
    return KosellClient(api_key=api_key), cfg


# --- ГЛАВНОЕ МЕНЮ KOSELL ---

@router.callback_query(F.data == "kosell:open")
async def kosell_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    client, cfg = await _get_kosell_client(account)

    bal_text = "⚠️ <i>API-ключ не привязан</i>"
    has_key = client is not None
    if client:
        try:
            bal = await client.get_balance()
            rub = bal.get("balance_rub", 0)
            usd = bal.get("balance_usd", 0)
            username = bal.get("username", "Пользователь")
            bal_text = f"👤 Аккаунт: <b>{html.escape(username)}</b>\n💰 Баланс: <b>{rub:,.2f} ₽</b> (<i>${usd:,.2f}</i>)"
        except Exception as exc:
            bal_text = f"❌ <i>Ошибка подключения: {html.escape(str(exc)[:60])}</i>"

    async with svc().db() as session:
        total_rules = await session.scalar(
            select(func.count(KosellLotRule.id)).where(KosellLotRule.account_id == account.id)
        ) or 0
        total_deals = await session.scalar(
            select(func.count(KosellDeal.id)).where(KosellDeal.account_id == account.id)
        ) or 0

    currency = cfg.currency if cfg else "RUB"
    auto_sent = cfg.auto_sent if cfg else True

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 " + ("Изменить API-ключ" if has_key else "Ввести API-ключ"), callback_data="kosell:apikey")],
        [InlineKeyboardButton(text=f"🎮 Каталог игр KOSell", callback_data="kosell:catalog:0")],
        [InlineKeyboardButton(text=f"🔗 Привязать лот Playerok ({total_rules})", callback_data="kosell:lots")],
        [InlineKeyboardButton(text="📋 Список привязок", callback_data="kosell:rules")],
        [InlineKeyboardButton(text=f"⚙️ Валюта: {currency}", callback_data="kosell:toggle_cur")],
        [InlineKeyboardButton(text=f"📊 История аренд ({total_deals})", callback_data="kosell:history:0")],
        [InlineKeyboardButton(text="⬅️ Меню плагинов", callback_data="plugins:mine")],
    ])

    text = (
        "🚀 <b>KOSell: Автоматическая аренда Steam-аккаунтов</b>\n\n"
        "Интеграция с сервисом <b>kosell.store</b>:\n"
        "• Мгновенная аренда игры по API при заказе на Playerok\n"
        "• Автоматическая выдача логина, пароля и срока\n"
        "• Покупатель получает код Steam Guard в чате по команде <code>#код</code>\n"
        "• Автоматический перевод сделки в статус «Выполнен»\n\n"
        f"<b>Статус KOSell API:</b>\n{bal_text}\n\n"
        f"📦 Привязано лотов: <b>{total_rules}</b> | Выдано аренд: <b>{total_deals}</b>"
    )
    await edit(call, text, markup)


# --- ВВОД API-КЛЮЧА ---

@router.callback_query(F.data == "kosell:apikey")
async def kosell_apikey_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(KosellState.api_key)
    text = (
        "🔑 <b>Настройка API-ключа KOSell</b>\n\n"
        "Получите API-ключ на сайте <a href=\"https://kosell.store/developers\">kosell.store/developers</a>\n"
        "и отправьте его в ответном сообщении:"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="kosell:open")]
    ])
    await edit(call, text, markup)


@router.message(KosellState.api_key)
async def kosell_apikey_save(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id if message.from_user else 0)
    if not account:
        await message.answer("❌ Аккаунт Playerok не выбран.")
        await state.clear()
        return

    raw_key = (message.text or "").strip()
    if not raw_key:
        await message.answer("⚠️ Ключ не может быть пустым.")
        return

    # Проверяем валидность ключа
    test_client = KosellClient(api_key=raw_key)
    try:
        bal = await test_client.get_balance()
        username = bal.get("username", "Пользователь")
        rub = bal.get("balance_rub", 0)
    except Exception as exc:
        await message.answer(
            f"❌ <b>Не удалось подключиться к KOSell:</b>\n<code>{html.escape(str(exc))}</code>\n\n"
            f"Проверьте правильность API-ключа и попробуйте снова:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="kosell:open")]
            ]),
        )
        return

    await state.clear()
    cipher = svc().cipher
    key_enc = cipher.encrypt(raw_key)

    async with svc().db() as session:
        cfg = await session.scalar(
            select(KosellAccountConfig).where(KosellAccountConfig.account_id == account.id)
        )
        if not cfg:
            cfg = KosellAccountConfig(
                account_id=account.id,
                api_key_encrypted=key_enc,
                currency="RUB",
                auto_sent=True,
                notify_seller=True,
            )
            session.add(cfg)
        else:
            cfg.api_key_encrypted = key_enc

        # Активируем плагин kosell_seller в plugin_states
        p_state = await session.scalar(
            select(PluginState).where(
                PluginState.account_id == account.id,
                PluginState.plugin_id == "kosell_seller",
            )
        )
        if not p_state:
            session.add(PluginState(account_id=account.id, plugin_id="kosell_seller", enabled=True, config={}))
        else:
            p_state.enabled = True

        await session.commit()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 К каталогу игр", callback_data="kosell:catalog:0")],
        [InlineKeyboardButton(text="🔗 Привязать лот", callback_data="kosell:lots")],
        [InlineKeyboardButton(text="🚀 В меню KOSell", callback_data="kosell:open")],
    ])

    await message.answer(
        f"✅ <b>API-ключ KOSell успешно сохранён!</b>\n\n"
        f"👤 Профиль: <b>{html.escape(username)}</b>\n"
        f"💰 Баланс: <b>{rub:,.2f} ₽</b>\n"
        f"Плагин автовыдачи KOSell успешно активирован!",
        reply_markup=markup,
    )


# --- ПЕРЕКЛЮЧЕНИЕ ВАЛЮТЫ ---

@router.callback_query(F.data == "kosell:toggle_cur")
async def kosell_toggle_currency(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        cfg = await session.scalar(
            select(KosellAccountConfig).where(KosellAccountConfig.account_id == account.id)
        )
        if cfg:
            cfg.currency = "USD" if cfg.currency == "RUB" else "RUB"
            await session.commit()
    await call.answer(f"Валюта списания переключена на {cfg.currency if cfg else 'RUB'}")
    await kosell_main_menu(call, FSMContext(None, None))


# --- КАТАЛОГ ИГР KOSELL ---

@router.callback_query(F.data.startswith("kosell:catalog:"))
async def kosell_catalog_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    client, cfg = await _get_kosell_client(account)
    if not client:
        await call.answer("Сначала введите API-ключ KOSell", show_alert=True)
        return

    page = int(call.data.rsplit(":", 1)[-1])
    per_page = 6
    currency = cfg.currency if cfg else "RUB"

    try:
        products = await client.get_products(currency=currency)
    except Exception as exc:
        await call.answer(f"Ошибка загрузки каталога: {exc}", show_alert=True)
        return

    total = len(products)
    start_idx = page * per_page
    paged = products[start_idx : start_idx + per_page]

    if not paged:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="kosell:open")]
        ])
        await edit(call, "🎮 В каталоге KOSell сейчас нет доступных игр.", markup)
        return

    b = InlineKeyboardBuilder()
    for prod in paged:
        name = clip(prod.get("name", "Игра"), 22)
        price = prod.get("price_per_hour", 0)
        avail = prod.get("available_accounts", 0)
        cur_sym = "₽" if currency == "RUB" else "$"
        b.button(
            text=f"🎮 {name} ({price}{cur_sym}/ч | {avail} шт.)",
            callback_data=f"kosell:prod:{prod['id']}",
        )
    b.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"kosell:catalog:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"kosell:catalog:{page + 1}"))
    if nav:
        b.row(*nav)

    b.row(InlineKeyboardButton(text="⬅️ В меню KOSell", callback_data="kosell:open"))

    text = (
        f"🎮 <b>Каталог игр KOSell (Стр. {page + 1})</b>\n\n"
        f"Всего игр в каталоге: <b>{total}</b>\n"
        f"Валюта цен: <b>{currency}</b>\n\n"
        f"<i>Нажмите на игру для подробностей и привязки к лоту Playerok.</i>"
    )
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("kosell:prod:"))
async def kosell_product_detail(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    client, cfg = await _get_kosell_client(account)
    if not client:
        return

    prod_id = int(call.data.rsplit(":", 1)[-1])
    currency = cfg.currency if cfg else "RUB"

    try:
        products = await client.get_products(currency=currency)
        prod = next((p for p in products if p["id"] == prod_id), None)
    except Exception as exc:
        await call.answer(f"Ошибка: {exc}", show_alert=True)
        return

    if not prod:
        await call.answer("Игра не найдена", show_alert=True)
        return

    cur_sym = "₽" if currency == "RUB" else "$"
    avail = prod.get("available_accounts", 0)
    avail_str = f"🟢 <b>{avail} шт. в наличии</b>" if avail > 0 else "🔴 <b>Нет свободных аккаунтов прямо сейчас</b>"

    next_avail_str = ""
    next_avails = prod.get("next_available") or []
    if next_avails and avail == 0:
        first = next_avails[0]
        next_avail_str = f"\n⏳ Ближайшее освобождение: <b>{html.escape(first.get('available_in_human', 'скоро'))}</b>"

    text = (
        f"🎮 <b>{html.escape(prod.get('name', 'Игра'))}</b>\n\n"
        f"🆔 ID в KOSell: <code>{prod['id']}</code>\n"
        f"💵 Цена за 1 час: <b>{prod.get('price_per_hour', 0):,.2f} {cur_sym}</b>\n"
        f"⏱ Лимиты аренды: от {prod.get('min_hours', 1)} до {prod.get('max_hours', 720)} ч.\n"
        f"📦 Статус: {avail_str}{next_avail_str}"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Привязать к лоту Playerok", callback_data=f"kosell:bind_game:{prod['id']}")],
        [InlineKeyboardButton(text="⬅️ К каталогу", callback_data="kosell:catalog:0")],
    ])
    await edit(call, text, markup)


# --- ПРИВЯЗКА ЛОТОВ PLAYEROK ---

@router.callback_query(F.data == "kosell:lots")
async def kosell_choose_lot(call: CallbackQuery) -> None:
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
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="kosell:open")]
        ])
        await edit(call, "📦 У вас нет активных лотов на Playerok.", markup)
        return

    b = InlineKeyboardBuilder()
    for item in items:
        b.button(text=f"📦 {clip(item.name, 26)}", callback_data=f"kosell:select_lot:{item.id}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="kosell:open"))

    text = (
        "🔗 <b>Привязка лота к аренде KOSell</b>\n\n"
        "Выберите ваш товар на Playerok, при покупке которого бот должен закупить аренду в KOSell:"
    )
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("kosell:select_lot:"))
async def kosell_select_lot_choose_game(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    lot_id = call.data.rsplit(":", 1)[-1]
    client, cfg = await _get_kosell_client(account)
    if not client:
        await call.answer("Сначала введите API-ключ KOSell", show_alert=True)
        return

    try:
        products = await client.get_products()
    except Exception as exc:
        await call.answer(f"Ошибка загрузки игр: {exc}", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    for p in products[:12]:
        b.button(text=f"🎮 {clip(p['name'], 24)}", callback_data=f"kosell:pair:{lot_id}:{p['id']}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="kosell:lots"))

    await edit(call, "🎮 <b>Выберите игру из KOSell для этого лота:</b>", b.as_markup())


@router.callback_query(F.data.startswith("kosell:bind_game:"))
async def kosell_bind_game_choose_lot(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    product_id = int(call.data.rsplit(":", 1)[-1])

    try:
        client = await svc().gateway.get_client(account)
        res = await client.call("get_my_items", statuses=None, count=24)
        items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        await call.answer(f"Ошибка загрузки лотов: {exc}", show_alert=True)
        return

    if not items:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="kosell:open")]
        ])
        await edit(call, "📦 У вас нет активных лотов на Playerok.", markup)
        return

    b = InlineKeyboardBuilder()
    for item in items:
        b.button(text=f"📦 {clip(item.name, 26)}", callback_data=f"kosell:pair:{item.id}:{product_id}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="kosell:catalog:0"))

    await edit(call, "🔗 <b>Выберите лот Playerok, к которому привязать эту игру:</b>", b.as_markup())


@router.callback_query(F.data.startswith("kosell:pair:"))
async def kosell_pair_confirm(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    parts = call.data.split(":")
    lot_id = parts[2]
    product_id = int(parts[3])

    client, _ = await _get_kosell_client(account)
    product_name = f"Игра #{product_id}"
    if client:
        try:
            prods = await client.get_products()
            p = next((x for x in prods if x["id"] == product_id), None)
            if p:
                product_name = p.get("name", product_name)
        except Exception:
            pass

    try:
        pok_client = await svc().gateway.get_client(account)
        item = await pok_client.get_item(lot_id)
        lot_title = getattr(item, "name", f"Лот {lot_id}")
    except Exception:
        lot_title = f"Лот {lot_id}"

    async with svc().db() as session:
        existing = await session.scalar(
            select(KosellLotRule).where(
                KosellLotRule.account_id == account.id,
                KosellLotRule.lot_id == lot_id,
            )
        )
        if existing:
            existing.product_id = product_id
            existing.product_name = product_name
            existing.lot_title = lot_title
            existing.enabled = True
            saved_rule = existing
        else:
            rule = KosellLotRule(
                account_id=account.id,
                lot_id=lot_id,
                lot_title=lot_title,
                product_id=product_id,
                product_name=product_name,
                duration_hours=1,
                enabled=True,
            )
            session.add(rule)
            saved_rule = rule
        await session.commit()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Настроить длительность (1ч по умолч.)", callback_data=f"kosell:rule_view:{saved_rule.id}")],
        [InlineKeyboardButton(text="📋 Список привязок", callback_data="kosell:rules")],
        [InlineKeyboardButton(text="🚀 В меню KOSell", callback_data="kosell:open")],
    ])

    await edit(
        call,
        f"✅ <b>Лот успешно привязан к KOSell!</b>\n\n"
        f"📦 Товар: <b>{html.escape(lot_title)}</b>\n"
        f"🎮 Игра KOSell: <b>{html.escape(product_name)}</b>\n\n"
        f"При каждой покупке лота бот автоматически арендует аккаунт в KOSell, "
        f"отправит данные покупателю и моментально выдаст Steam Guard код по команде <code>#код</code>!",
        markup,
    )


# --- СПИСОК И УПРАВЛЕНИЕ ПРИВЯЗКАМИ ---

@router.callback_query(F.data == "kosell:rules")
async def kosell_rules_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        rules = (await session.scalars(
            select(KosellLotRule)
            .where(KosellLotRule.account_id == account.id)
            .order_by(KosellLotRule.id.desc())
        )).all()

    if not rules:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Привязать лот", callback_data="kosell:lots")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="kosell:open")],
        ])
        await edit(call, "📋 Пока нет привязанных лотов к KOSell.", markup)
        return

    b = InlineKeyboardBuilder()
    for r in rules:
        st_icon = "🟢" if r.enabled else "⏸"
        title = f"{st_icon} {clip(r.lot_title, 18)} -> {clip(r.product_name, 12)} ({r.duration_hours}ч)"
        b.button(text=title, callback_data=f"kosell:rule_view:{r.id}")
        b.button(text="🗑", callback_data=f"kosell:rule_del:{r.id}")
    b.adjust(2)

    b.row(InlineKeyboardButton(text="➕ Привязать ещё лот", callback_data="kosell:lots"))
    b.row(InlineKeyboardButton(text="⬅️ В меню KOSell", callback_data="kosell:open"))

    text = (
        "📋 <b>Привязанные лоты KOSell</b>\n\n"
        "🟢 — Активен (автовыдача работает)\n"
        "⏸ — На паузе\n"
        "<i>Нажмите на строку для настройки часов аренды или шаблона.</i>"
    )
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("kosell:rule_view:"))
async def kosell_rule_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(KosellLotRule, rule_id)
        if not rule or rule.account_id != account.id:
            await call.answer("Правило не найдено", show_alert=True)
            return

    st_str = "🟢 Включено" if rule.enabled else "⏸ На паузе"
    text = (
        f"⚙️ <b>Настройка привязки #{rule.id}</b>\n\n"
        f"📦 Товар: <b>{html.escape(rule.lot_title)}</b>\n"
        f"🎮 Игра KOSell: <b>{html.escape(rule.product_name)}</b> (ID: {rule.product_id})\n"
        f"⏱ Длительность аренды: <b>{rule.duration_hours} ч.</b>\n"
        f"Статус: <b>{st_str}</b>"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏱ 1 час", callback_data=f"kosell:set_hours:{rule.id}:1"),
            InlineKeyboardButton(text="⏱ 2 часа", callback_data=f"kosell:set_hours:{rule.id}:2"),
            InlineKeyboardButton(text="⏱ 3 часа", callback_data=f"kosell:set_hours:{rule.id}:3"),
        ],
        [
            InlineKeyboardButton(text="⏱ 6 часов", callback_data=f"kosell:set_hours:{rule.id}:6"),
            InlineKeyboardButton(text="⏱ 12 часов", callback_data=f"kosell:set_hours:{rule.id}:12"),
            InlineKeyboardButton(text="⏱ 24 часа", callback_data=f"kosell:set_hours:{rule.id}:24"),
        ],
        [InlineKeyboardButton(text="⏸ Включить/Пауза", callback_data=f"kosell:rule_toggle:{rule.id}")],
        [InlineKeyboardButton(text="🗑 Удалить привязку", callback_data=f"kosell:rule_del:{rule.id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="kosell:rules")],
    ])
    await edit(call, text, markup)


@router.callback_query(F.data.startswith("kosell:set_hours:"))
async def kosell_rule_set_hours(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    parts = call.data.split(":")
    rule_id = int(parts[2])
    hours = int(parts[3])

    async with svc().db() as session:
        rule = await session.get(KosellLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.duration_hours = hours
            await session.commit()

    await call.answer(f"Установлена длительность: {hours} ч.")
    await kosell_rule_view(call)


@router.callback_query(F.data.startswith("kosell:rule_toggle:"))
async def kosell_rule_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(KosellLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.enabled = not rule.enabled
            await session.commit()
            await call.answer("Включено" if rule.enabled else "Поставлено на паузу")

    await kosell_rule_view(call)


@router.callback_query(F.data.startswith("kosell:rule_del:"))
async def kosell_rule_delete(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        await session.execute(
            delete(KosellLotRule).where(
                KosellLotRule.id == rule_id,
                KosellLotRule.account_id == account.id,
            )
        )
        await session.commit()

    await call.answer("Привязка удалена")
    await kosell_rules_list(call)


# --- ИСТОРИЯ ВЫДАННЫХ АРЕНД ---

@router.callback_query(F.data.startswith("kosell:history:"))
async def kosell_history_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    page = int(call.data.rsplit(":", 1)[-1])
    per_page = 6

    async with svc().db() as session:
        deals = (await session.scalars(
            select(KosellDeal)
            .where(KosellDeal.account_id == account.id)
            .order_by(KosellDeal.id.desc())
            .offset(page * per_page)
            .limit(per_page + 1)
        )).all()

    has_next = len(deals) > per_page
    deals = deals[:per_page]

    if not deals:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="kosell:open")]
        ])
        await edit(call, "📊 История аренд пока пуста.", markup)
        return

    lines = []
    for d in deals:
        exp_str = d.expires_at.strftime("%d.%m %H:%M") if d.expires_at else f"{d.duration_hours} ч."
        lines.append(
            f"• <b>{html.escape(d.product_name)}</b> ({d.duration_hours}ч)\n"
            f"  Логин: <code>{html.escape(d.steam_login)}</code> | Сделка: <code>{d.deal_id}</code>\n"
            f"  Срок: до {exp_str} | Запросов Guard: {d.code_requests_count}"
        )

    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"kosell:history:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"kosell:history:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ В меню KOSell", callback_data="kosell:open"))

    text = f"📊 <b>История выданных аренд KOSell (Стр. {page + 1})</b>\n\n" + "\n\n".join(lines)
    await edit(call, text, b.as_markup())
