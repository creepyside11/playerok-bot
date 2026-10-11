from __future__ import annotations

import html
import logging
import re
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select

from .autosmm_manager import (
    AutoSmmConfig,
    AutoSmmLotRule,
    AutoSmmOrder,
    SmmApiClient,
)
from .handlers import active_account, clip, edit, require_account, svc
from .models import PlayerokAccount
from .plugin_system import PluginState
from .states import State, StatesGroup

logger = logging.getLogger("autosmm_ui")
router = Router(name="autosmm_ui")


class AutoSmmState(StatesGroup):
    api_url = State()
    api_key = State()
    custom_service_id = State()
    custom_quantity = State()


async def _get_smm_client(account: PlayerokAccount) -> tuple[SmmApiClient | None, AutoSmmConfig | None]:
    async with svc().db() as session:
        cfg = await session.scalar(
            select(AutoSmmConfig).where(AutoSmmConfig.account_id == account.id)
        )
    if not cfg or not cfg.api_key_encrypted:
        return None, cfg

    cipher = svc().cipher
    try:
        api_key = cipher.decrypt(cfg.api_key_encrypted) if cipher else cfg.api_key_encrypted
    except Exception:
        api_key = cfg.api_key_encrypted

    return SmmApiClient(api_url=cfg.api_url, api_key=api_key), cfg


# --- ГЛАВНОЕ МЕНЮ AUTOSMM ---

@router.callback_query(F.data == "autosmm:open")
async def autosmm_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    client, cfg = await _get_smm_client(account)

    bal_text = "⚠️ <i>API-ключ не настроен</i>"
    has_key = client is not None
    if client:
        try:
            bal = await client.get_balance()
            balance = bal.get("balance", "—")
            currency = bal.get("currency", "RUB")
            bal_text = f"🌐 URL: <code>{html.escape(client.api_url)}</code>\n💰 Баланс: <b>{balance} {currency}</b>"
        except Exception as exc:
            bal_text = f"🌐 URL: <code>{html.escape(client.api_url)}</code>\n❌ <i>Ошибка подключения: {html.escape(str(exc)[:60])}</i>"

    async with svc().db() as session:
        total_rules = await session.scalar(
            select(func.count(AutoSmmLotRule.id)).where(AutoSmmLotRule.account_id == account.id)
        ) or 0
        total_orders = await session.scalar(
            select(func.count(AutoSmmOrder.id)).where(AutoSmmOrder.account_id == account.id)
        ) or 0

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 " + ("Изменить API-ключ" if has_key else "Ввести API-ключ"), callback_data="autosmm:apikey")],
        [InlineKeyboardButton(text="🌐 Настроить API URL", callback_data="autosmm:apiurl")],
        [InlineKeyboardButton(text="📋 Каталог услуг SMM", callback_data="autosmm:services:0")],
        [InlineKeyboardButton(text=f"🔗 Привязать лот Playerok ({total_rules})", callback_data="autosmm:lots")],
        [InlineKeyboardButton(text="⚙️ Список привязок", callback_data="autosmm:rules")],
        [InlineKeyboardButton(text=f"📊 История заказов ({total_orders})", callback_data="autosmm:history:0")],
        [InlineKeyboardButton(text="⬅️ Меню плагинов", callback_data="plugins:mine")],
    ])

    text = (
        "🚀 <b>AutoSMM: Автоматическая накрутка (SMM Panel v2)</b>\n\n"
        "Интеграция с сервисами SMM (smmway и любыми SMM v2 панелями):\n"
        "• Покупатель оплачивает лот на Playerok (подписчики, лайки, просмотры)\n"
        "• Бот сам извлекает ссылку из заказа и мгновенно создает задачу в SMM панели\n"
        "• Заказ на Playerok автоматически переводится в статус «Выполнен»\n\n"
        f"<b>Статус панели:</b>\n{bal_text}\n\n"
        f"📦 Привязано лотов: <b>{total_rules}</b> | Отправлено заказов: <b>{total_orders}</b>"
    )
    await edit(call, text, markup)


# --- НАСТРОЙКА КЛЮЧА И URL ---

@router.callback_query(F.data == "autosmm:apiurl")
async def autosmm_apiurl_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoSmmState.api_url)
    text = (
        "🌐 <b>Введите URL API вашей SMM панели</b>\n\n"
        "По умолчанию используется <code>https://smmway.com/api/v2</code>\n"
        "Подходит любая панель со стандартным API v2 (action=add, services, balance).\n\n"
        "Отправьте адрес в ответном сообщении или нажмите «Оставить по умолчанию»:"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 smmway.com (по умолчанию)", callback_data="autosmm:set_default_url")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="autosmm:open")],
    ])
    await edit(call, text, markup)


@router.callback_query(F.data == "autosmm:set_default_url")
async def autosmm_set_default_url_cb(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.clear()
    async with svc().db() as session:
        cfg = await session.scalar(select(AutoSmmConfig).where(AutoSmmConfig.account_id == account.id))
        if cfg:
            cfg.api_url = "https://smmway.com/api/v2"
        else:
            session.add(AutoSmmConfig(account_id=account.id, api_url="https://smmway.com/api/v2"))
        await session.commit()
    await call.answer("Установлен URL smmway.com")
    await autosmm_main_menu(call, state)


@router.message(AutoSmmState.api_url)
async def autosmm_apiurl_save(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id if message.from_user else 0)
    if not account:
        await message.answer("❌ Аккаунт Playerok не выбран.")
        await state.clear()
        return

    url = (message.text or "").strip()
    if not url.startswith("http"):
        await message.answer("⚠️ URL должен начинаться с http:// или https://")
        return

    await state.clear()
    async with svc().db() as session:
        cfg = await session.scalar(select(AutoSmmConfig).where(AutoSmmConfig.account_id == account.id))
        if cfg:
            cfg.api_url = url
        else:
            session.add(AutoSmmConfig(account_id=account.id, api_url=url))
        await session.commit()

    await message.answer(
        f"✅ <b>API URL успешно сохранён:</b>\n<code>{html.escape(url)}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 В меню AutoSMM", callback_data="autosmm:open")]
        ])
    )


@router.callback_query(F.data == "autosmm:apikey")
async def autosmm_apikey_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoSmmState.api_key)
    text = (
        "🔑 <b>Настройка API-токена SMM панели</b>\n\n"
        "Скопируйте ваш API Key / Token в личном кабинете SMM панели и отправьте в ответном сообщении:"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="autosmm:open")]
    ])
    await edit(call, text, markup)


@router.message(AutoSmmState.api_key)
async def autosmm_apikey_save(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id if message.from_user else 0)
    if not account:
        await message.answer("❌ Аккаунт Playerok не выбран.")
        await state.clear()
        return

    raw_key = (message.text or "").strip()
    if not raw_key:
        await message.answer("⚠️ Ключ не может быть пустым.")
        return

    async with svc().db() as session:
        cfg = await session.scalar(select(AutoSmmConfig).where(AutoSmmConfig.account_id == account.id))
        api_url = cfg.api_url if cfg else "https://smmway.com/api/v2"

    # Проверяем ключ
    client = SmmApiClient(api_url=api_url, api_key=raw_key)
    try:
        bal = await client.get_balance()
        balance = bal.get("balance", "0")
        currency = bal.get("currency", "RUB")
    except Exception as exc:
        await message.answer(
            f"❌ <b>Не удалось подключиться к SMM панели:</b>\n<code>{html.escape(str(exc))}</code>\n\n"
            f"Проверьте API-ключ и URL панели и попробуйте снова:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="autosmm:open")]
            ])
        )
        return

    await state.clear()
    cipher = svc().cipher
    key_enc = cipher.encrypt(raw_key)

    async with svc().db() as session:
        cfg = await session.scalar(select(AutoSmmConfig).where(AutoSmmConfig.account_id == account.id))
        if not cfg:
            cfg = AutoSmmConfig(
                account_id=account.id,
                api_url=api_url,
                api_key_encrypted=key_enc,
                auto_sent=True,
                notify_seller=True,
            )
            session.add(cfg)
        else:
            cfg.api_key_encrypted = key_enc

        # Активируем плагин autosmm в plugin_states
        p_state = await session.scalar(
            select(PluginState).where(
                PluginState.account_id == account.id,
                PluginState.plugin_id == "autosmm",
            )
        )
        if not p_state:
            session.add(PluginState(account_id=account.id, plugin_id="autosmm", enabled=True, config={}))
        else:
            p_state.enabled = True

        await session.commit()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 К каталогу услуг", callback_data="autosmm:services:0")],
        [InlineKeyboardButton(text="🔗 Привязать лот", callback_data="autosmm:lots")],
        [InlineKeyboardButton(text="🚀 В меню AutoSMM", callback_data="autosmm:open")],
    ])

    await message.answer(
        f"✅ <b>API-ключ SMM успешно сохранён!</b>\n\n"
        f"💰 Баланс: <b>{balance} {currency}</b>\n"
        f"Плагин автонакрутки активирован!",
        reply_markup=markup,
    )


# --- КАТАЛОГ УСЛУГ SMM ---

@router.callback_query(F.data.startswith("autosmm:services:"))
async def autosmm_services_catalog(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    client, _ = await _get_smm_client(account)
    if not client:
        await call.answer("Сначала введите API-ключ", show_alert=True)
        return

    page = int(call.data.rsplit(":", 1)[-1])
    per_page = 6

    try:
        services = await client.get_services()
    except Exception as exc:
        await call.answer(f"Ошибка загрузки услуг: {exc}", show_alert=True)
        return

    total = len(services)
    start_idx = page * per_page
    paged = services[start_idx : start_idx + per_page]

    if not paged:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="autosmm:open")]
        ])
        await edit(call, "📋 В панели пока нет услуг или они недоступны.", markup)
        return

    b = InlineKeyboardBuilder()
    for srv in paged:
        srv_id = srv.get("service") or srv.get("id")
        name = clip(str(srv.get("name", "Услуга")), 22)
        rate = srv.get("rate", "—")
        b.button(
            text=f"⚡ #{srv_id} {name} ({rate}₽/1к)",
            callback_data=f"autosmm:bind_srv:{srv_id}",
        )
    b.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"autosmm:services:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"autosmm:services:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ В меню AutoSMM", callback_data="autosmm:open"))

    text = (
        f"📋 <b>Каталог услуг SMM панели (Стр. {page + 1})</b>\n\n"
        f"Всего услуг: <b>{total}</b>\n"
        f"<i>Нажмите на услугу, чтобы сразу привязать её к лоту Playerok.</i>"
    )
    await edit(call, text, b.as_markup())


# --- ПРИВЯЗКА ЛОТОВ PLAYEROK ---

@router.callback_query(F.data == "autosmm:lots")
async def autosmm_choose_lot(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    try:
        pok_client = await svc().gateway.get_client(account)
        res = await pok_client.call("get_my_items", statuses=None, count=24)
        items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        await call.answer(f"Ошибка загрузки лотов: {exc}", show_alert=True)
        return

    if not items:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="autosmm:open")]
        ])
        await edit(call, "📦 У вас нет активных лотов на Playerok.", markup)
        return

    b = InlineKeyboardBuilder()
    for item in items:
        b.button(text=f"📦 {clip(item.name, 26)}", callback_data=f"autosmm:select_lot:{item.id}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="autosmm:open"))

    text = "🔗 <b>Выберите лот Playerok для автоматической накрутки:</b>"
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("autosmm:bind_srv:"))
async def autosmm_bind_srv_choose_lot(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    service_id = call.data.rsplit(":", 1)[-1]
    try:
        pok_client = await svc().gateway.get_client(account)
        res = await pok_client.call("get_my_items", statuses=None, count=24)
        items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        await call.answer(f"Ошибка: {exc}", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    for item in items:
        b.button(text=f"📦 {clip(item.name, 26)}", callback_data=f"autosmm:pair:{item.id}:{service_id}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="autosmm:services:0"))

    await edit(call, f"🔗 <b>К какому лоту привязать услугу #{service_id}?</b>", b.as_markup())


@router.callback_query(F.data.startswith("autosmm:select_lot:"))
async def autosmm_select_lot_choose_service(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    lot_id = call.data.rsplit(":", 1)[-1]
    client, _ = await _get_smm_client(account)
    if not client:
        await call.answer("Сначала настройте API-ключ", show_alert=True)
        return

    try:
        services = await client.get_services()
    except Exception as exc:
        await call.answer(f"Ошибка: {exc}", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    for srv in services[:12]:
        srv_id = srv.get("service") or srv.get("id")
        name = clip(str(srv.get("name", "Услуга")), 24)
        b.button(text=f"⚡ #{srv_id} {name}", callback_data=f"autosmm:pair:{lot_id}:{srv_id}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="autosmm:lots"))

    await edit(call, "⚡ <b>Выберите услугу SMM для этого лота:</b>", b.as_markup())


@router.callback_query(F.data.startswith("autosmm:pair:"))
async def autosmm_pair_confirm(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    parts = call.data.split(":")
    lot_id = parts[2]
    service_id = str(parts[3])

    client, _ = await _get_smm_client(account)
    service_name = f"Услуга #{service_id}"
    if client:
        try:
            services = await client.get_services()
            s = next((x for x in services if str(x.get("service") or x.get("id")) == service_id), None)
            if s:
                service_name = s.get("name", service_name)
        except Exception:
            pass

    try:
        pok_client = await svc().gateway.get_client(account)
        item = await pok_client.get_item(lot_id)
        lot_title = getattr(item, "name", f"Лот {lot_id}")
    except Exception:
        lot_title = f"Лот {lot_id}"

    async with svc().db() as session:
        rule = await session.scalar(
            select(AutoSmmLotRule).where(
                AutoSmmLotRule.account_id == account.id,
                AutoSmmLotRule.lot_id == lot_id,
            )
        )
        if rule:
            rule.service_id = service_id
            rule.service_name = service_name
            rule.lot_title = lot_title
            rule.enabled = True
        else:
            rule = AutoSmmLotRule(
                account_id=account.id,
                lot_id=lot_id,
                lot_title=lot_title,
                service_id=service_id,
                service_name=service_name,
                quantity=100,
                enabled=True,
            )
            session.add(rule)
        await session.commit()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Настроить количество (100 по умолч.)", callback_data=f"autosmm:rule_view:{rule.id}")],
        [InlineKeyboardButton(text="⚙️ Список привязок", callback_data="autosmm:rules")],
        [InlineKeyboardButton(text="🚀 В меню AutoSMM", callback_data="autosmm:open")],
    ])

    await edit(
        call,
        f"✅ <b>Лот успешно привязан к SMM панели!</b>\n\n"
        f"📦 Товар: <b>{html.escape(lot_title)}</b>\n"
        f"⚡ Услуга: <b>{html.escape(service_name)}</b> (#{service_id})\n"
        f"🔢 Количество: <b>{rule.quantity} шт.</b>\n\n"
        f"При покупке лота бот автоматически найдёт ссылку покупателя и запустит накрутку!",
        markup,
    )


# --- СПИСОК ПРИВЯЗОК ---

@router.callback_query(F.data == "autosmm:rules")
async def autosmm_rules_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        rules = (await session.scalars(
            select(AutoSmmLotRule).where(AutoSmmLotRule.account_id == account.id).order_by(AutoSmmLotRule.id.desc())
        )).all()

    if not rules:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Привязать лот", callback_data="autosmm:lots")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="autosmm:open")],
        ])
        await edit(call, "📋 Пока нет привязанных лотов к AutoSMM.", markup)
        return

    b = InlineKeyboardBuilder()
    for r in rules:
        st = "🟢" if r.enabled else "⏸"
        title = f"{st} {clip(r.lot_title, 18)} -> #{r.service_id} ({r.quantity} шт.)"
        b.button(text=title, callback_data=f"autosmm:rule_view:{r.id}")
        b.button(text="🗑", callback_data=f"autosmm:rule_del:{r.id}")
    b.adjust(2)

    b.row(InlineKeyboardButton(text="➕ Привязать ещё лот", callback_data="autosmm:lots"))
    b.row(InlineKeyboardButton(text="⬅️ В меню AutoSMM", callback_data="autosmm:open"))

    text = "📋 <b>Привязанные лоты AutoSMM:</b>\n\n<i>Нажмите на строку для изменения количества накрутки.</i>"
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("autosmm:rule_view:"))
async def autosmm_rule_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(AutoSmmLotRule, rule_id)
        if not rule or rule.account_id != account.id:
            await call.answer("Не найдено", show_alert=True)
            return

    st = "🟢 Включено" if rule.enabled else "⏸ На паузе"
    text = (
        f"⚙️ <b>Настройка привязки #{rule.id}</b>\n\n"
        f"📦 Товар: <b>{html.escape(rule.lot_title)}</b>\n"
        f"⚡ Услуга: <b>{html.escape(rule.service_name or rule.service_id)}</b> (#{rule.service_id})\n"
        f"🔢 Количество: <b>{rule.quantity} шт.</b>\n"
        f"Статус: <b>{st}</b>"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="50", callback_data=f"autosmm:set_qty:{rule.id}:50"),
            InlineKeyboardButton(text="100", callback_data=f"autosmm:set_qty:{rule.id}:100"),
            InlineKeyboardButton(text="250", callback_data=f"autosmm:set_qty:{rule.id}:250"),
        ],
        [
            InlineKeyboardButton(text="500", callback_data=f"autosmm:set_qty:{rule.id}:500"),
            InlineKeyboardButton(text="1 000", callback_data=f"autosmm:set_qty:{rule.id}:1000"),
            InlineKeyboardButton(text="5 000", callback_data=f"autosmm:set_qty:{rule.id}:5000"),
        ],
        [InlineKeyboardButton(text="⏸ Включить/Пауза", callback_data=f"autosmm:rule_toggle:{rule.id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"autosmm:rule_del:{rule.id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="autosmm:rules")],
    ])
    await edit(call, text, markup)


@router.callback_query(F.data.startswith("autosmm:set_qty:"))
async def autosmm_set_qty(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    parts = call.data.split(":")
    rule_id = int(parts[2])
    qty = int(parts[3])

    async with svc().db() as session:
        rule = await session.get(AutoSmmLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.quantity = qty
            await session.commit()

    await call.answer(f"Количество установлено: {qty}")
    await autosmm_rule_view(call)


@router.callback_query(F.data.startswith("autosmm:rule_toggle:"))
async def autosmm_rule_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(AutoSmmLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.enabled = not rule.enabled
            await session.commit()
            await call.answer("Включено" if rule.enabled else "На паузе")

    await autosmm_rule_view(call)


@router.callback_query(F.data.startswith("autosmm:rule_del:"))
async def autosmm_rule_del(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        await session.execute(
            delete(AutoSmmLotRule).where(
                AutoSmmLotRule.id == rule_id,
                AutoSmmLotRule.account_id == account.id,
            )
        )
        await session.commit()

    await call.answer("Удалено")
    await autosmm_rules_list(call)


# --- ИСТОРИЯ ЗАКАЗОВ ---

@router.callback_query(F.data.startswith("autosmm:history:"))
async def autosmm_history(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    page = int(call.data.rsplit(":", 1)[-1])
    per_page = 6

    async with svc().db() as session:
        orders = (await session.scalars(
            select(AutoSmmOrder)
            .where(AutoSmmOrder.account_id == account.id)
            .order_by(AutoSmmOrder.id.desc())
            .offset(page * per_page)
            .limit(per_page + 1)
        )).all()

    has_next = len(orders) > per_page
    orders = orders[:per_page]

    if not orders:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="autosmm:open")]
        ])
        await edit(call, "📊 История заказов SMM пока пуста.", markup)
        return

    lines = []
    for o in orders:
        lines.append(
            f"• <b>Заказ #{o.smm_order_id}</b> (Сделка: <code>{o.deal_id}</code>)\n"
            f"  Услуга: #{o.service_id} | Кол-во: <b>{o.quantity}</b>\n"
            f"  Ссылка: <code>{clip(o.link, 30)}</code> | Статус: <i>{o.status}</i>"
        )

    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"autosmm:history:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"autosmm:history:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ В меню AutoSMM", callback_data="autosmm:open"))

    text = f"📊 <b>История заказов SMM (Стр. {page + 1})</b>\n\n" + "\n\n".join(lines)
    await edit(call, text, b.as_markup())
