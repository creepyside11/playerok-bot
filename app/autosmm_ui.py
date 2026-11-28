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
    SmmApiClient, SmmError, bind_service, get_configured_client, get_owned_order,
    normalize_api_url, provider_service, refresh_order, request_order_refill, valid_keyword,
    valid_service_id, validate_quantity,
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
    keyword = State()
    custom_service_id = State()
    custom_quantity = State()


async def _get_smm_client(account: PlayerokAccount) -> tuple[SmmApiClient | None, AutoSmmConfig | None]:
    try:
        return await get_configured_client(svc().db, svc().cipher, account.id)
    except SmmError:
        return None, None


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
        [InlineKeyboardButton(text="🔤 Привязать по ключевому слову", callback_data="autosmm:keyword")],
        [InlineKeyboardButton(text="⚙️ Список привязок", callback_data="autosmm:rules")],
        [InlineKeyboardButton(text=f"📊 История заказов ({total_orders})", callback_data="autosmm:history:0")],
        [InlineKeyboardButton(text="⬅️ Меню плагинов", callback_data="plugins:mine")],
    ])

    text = (
        "🚀 <b>AutoSMM: Автоматическая накрутка (SMM Panel v2)</b>\n\n"
        "Интеграция с сервисами SMM (smmway и любыми SMM v2 панелями):\n"
        "• Покупатель оплачивает лот на Playerok (подписчики, лайки, просмотры)\n"
        "• Бот сам извлекает ссылку из заказа и мгновенно создает задачу в SMM панели\n"
        "• Статус заказа и доступность refill можно проверить в истории\n\n"
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
    try:
        url = normalize_api_url(url)
    except SmmError as exc:
        await message.answer(html.escape(str(exc)))
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
    try:
        client = SmmApiClient(api_url=api_url, api_key=raw_key)
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
    b.row(InlineKeyboardButton(text="🔤 По ключевой фразе", callback_data=f"autosmm:keyword_srv:{service_id}"))
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
    if not client:
        await call.answer("Настройте API панели", show_alert=True)
        return
    try:
        service = provider_service(await client.get_services(), service_id)
        if not service:
            raise SmmError("Услуга не найдена.")
        pok_client = await svc().gateway.get_client(account)
        from .miniapp import MiniApp
        item = await MiniApp.owned_listing(pok_client, account, lot_id)
        lot_title = str(item.name or "Лот")
        async with svc().db() as session:
            old = await session.scalar(select(AutoSmmLotRule).where(
                AutoSmmLotRule.account_id == account.id, AutoSmmLotRule.lot_id == lot_id,
            ))
        quantity = old.quantity if old else 100
        try:
            quantity = validate_quantity(quantity, service)
        except SmmError:
            minimum, maximum = int(service.get("min") or 1), int(service.get("max") or 10_000_000)
            quantity = validate_quantity(max(minimum, min(100, maximum)), service)
        rule = await bind_service(svc().db, account.id, service=service, quantity=quantity,
                                  lot_id=lot_id, lot_title=lot_title)
        service_name = rule.service_name
    except Exception as exc:
        await call.answer(str(exc)[:180], show_alert=True)
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Настроить количество", callback_data=f"autosmm:rule_view:{rule.id}")],
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


@router.callback_query((F.data == "autosmm:keyword") | F.data.startswith("autosmm:keyword_srv:"))
async def autosmm_keyword_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    client, _ = await _get_smm_client(account)
    if not client:
        await call.answer("Сначала настройте API-ключ и URL", show_alert=True)
        return
    selected = call.data.rsplit(":", 1)[-1] if call.data.startswith("autosmm:keyword_srv:") else None
    await state.clear()
    await state.update_data(account_id=str(account.id), selected_service=selected)
    await state.set_state(AutoSmmState.keyword)
    await edit(call, "🔤 <b>Введите ключевое слово или фразу из названия лота.</b>\n"
               "Регистр не важен; точный ID лота имеет приоритет. Более длинная фраза выигрывает при совпадении.",
               InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="autosmm:open")]]))


async def wizard_account(message: Message, state: FSMContext) -> PlayerokAccount | None:
    account = await active_account(message.from_user.id if message.from_user else 0)
    data = await state.get_data()
    if not account or data.get("account_id") != str(account.id):
        await state.clear()
        await message.answer("Аккаунт изменился. Откройте AutoSMM заново.")
        return None
    return account


@router.message(AutoSmmState.keyword, F.text)
async def autosmm_keyword_value(message: Message, state: FSMContext) -> None:
    if not await wizard_account(message, state):
        return
    if not valid_keyword(message.text):
        await message.answer("Введите фразу от 2 до 128 символов.")
        return
    await state.update_data(keyword=message.text.strip())
    data = await state.get_data()
    if data.get("selected_service") and valid_service_id(data["selected_service"]):
        await autosmm_service_selected(message, state, data["selected_service"])
    else:
        await state.set_state(AutoSmmState.custom_service_id)
        await message.answer("⚡ Введите ID услуги из каталога SMM-панели.")


async def autosmm_service_selected(message: Message, state: FSMContext, service_id: str) -> None:
    account = await wizard_account(message, state)
    if not account:
        return
    try:
        if not valid_service_id(service_id):
            raise SmmError("Некорректный ID услуги.")
        client, _ = await _get_smm_client(account)
        if not client:
            raise SmmError("Настройте API-ключ.")
        service = provider_service(await client.get_services(), service_id)
        if not service:
            raise SmmError("Услуга не найдена в каталоге.")
    except Exception as exc:
        await state.set_state(AutoSmmState.custom_service_id)
        await message.answer(html.escape(str(exc)[:250]) + " Введите ID ещё раз:")
        return
    await state.update_data(selected_service=service_id)
    await state.set_state(AutoSmmState.custom_quantity)
    await message.answer(
        f"🔢 <b>{html.escape(str(service.get('name') or service_id)[:256])}</b>\n"
        f"Введите количество (мин.: {html.escape(str(service.get('min', '—')))}, "
        f"макс.: {html.escape(str(service.get('max', '—')))})."
    )


@router.message(AutoSmmState.custom_service_id, F.text)
async def autosmm_service_id_value(message: Message, state: FSMContext) -> None:
    await autosmm_service_selected(message, state, (message.text or "").strip())


@router.message(AutoSmmState.custom_quantity, F.text)
async def autosmm_custom_quantity(message: Message, state: FSMContext) -> None:
    account = await wizard_account(message, state)
    if not account:
        return
    data = await state.get_data()
    try:
        value = (message.text or "").strip()
        if not value.isascii() or not value.isdigit() or len(value) > 8:
            raise SmmError("Введите целое количество.")
        quantity = int(value)
        client, _ = await _get_smm_client(account)
        if not client:
            raise SmmError("API-ключ не настроен.")
        if data.get("rule_id"):
            async with svc().db() as session:
                row = await session.get(AutoSmmLotRule, data["rule_id"])
                if not row or row.account_id != account.id:
                    raise SmmError("Привязка не найдена.")
                service_id = row.service_id
            service = provider_service(await client.get_services(), service_id)
            if not service:
                raise SmmError("Услуга больше недоступна.")
            quantity = validate_quantity(quantity, service)
            async with svc().db() as session:
                row = await session.get(AutoSmmLotRule, data["rule_id"], with_for_update=True)
                if not row or row.account_id != account.id or row.service_id != service_id:
                    raise SmmError("Привязка изменилась.")
                row.quantity = quantity
                await session.commit()
        else:
            service = provider_service(await client.get_services(), data.get("selected_service"))
            if not service:
                raise SmmError("Услуга больше недоступна.")
            row = await bind_service(svc().db, account.id, service=service, quantity=quantity, keyword=data.get("keyword"))
    except (ValueError, TypeError) as exc:
        await message.answer(html.escape(str(exc)[:250]) + " Введите количество ещё раз:")
        return
    except Exception:
        await message.answer("Не удалось получить каталог SMM. Попробуйте позже.")
        return
    await state.clear()
    await message.answer(
        f"✅ Привязка сохранена: <b>{html.escape(row.lot_title)}</b> → #{html.escape(row.service_id)}, {row.quantity} шт.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Настроить привязку", callback_data=f"autosmm:rule_view:{row.id}")],
            [InlineKeyboardButton(text="Список привязок", callback_data="autosmm:rules")],
        ]),
    )


@router.callback_query(F.data.startswith("autosmm:qty_custom:"))
async def autosmm_custom_qty_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        row = await session.get(AutoSmmLotRule, rule_id)
    if not row or row.account_id != account.id:
        await call.answer("Привязка не найдена", show_alert=True)
        return
    await state.clear()
    await state.update_data(account_id=str(account.id), rule_id=rule_id)
    await state.set_state(AutoSmmState.custom_quantity)
    await edit(call, f"Введите количество для услуги #{html.escape(row.service_id)}:",
               InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="autosmm:open")]]))


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
            [InlineKeyboardButton(text="🔤 Привязать по слову", callback_data="autosmm:keyword")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="autosmm:open")],
        ])
        await edit(call, "📋 Пока нет привязанных лотов к AutoSMM.", markup)
        return

    b = InlineKeyboardBuilder()
    for r in rules:
        st = "🟢" if r.enabled else "⏸"
        title = f"{st} {'🔤 ' if r.keyword else '📦 '}{clip(r.lot_title, 18)} → #{r.service_id} ({r.quantity})"
        b.button(text=title, callback_data=f"autosmm:rule_view:{r.id}")
        b.button(text="🗑", callback_data=f"autosmm:rule_del:{r.id}")
    b.adjust(2)

    b.row(InlineKeyboardButton(text="➕ Привязать ещё лот", callback_data="autosmm:lots"))
    b.row(InlineKeyboardButton(text="🔤 Привязать по слову", callback_data="autosmm:keyword"))
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
        f"{'🔤 Фраза' if rule.keyword else '📦 Товар'}: <b>{html.escape(rule.lot_title)}</b>\n"
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
        [InlineKeyboardButton(text="🔢 Своё количество", callback_data=f"autosmm:qty_custom:{rule.id}")],
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

    try:
        from .miniapp_autosmm import AutoSmmPanel
        await AutoSmmPanel(svc().db, svc().cipher).update_rule(account.id, rule_id, {"quantity": qty})
    except Exception as exc:
        await call.answer(str(exc)[:180], show_alert=True)
        return

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
            f"• <b>Заказ #{html.escape(o.smm_order_id)}</b> (Сделка: <code>{html.escape(o.deal_id)}</code>)\n"
            f"  Услуга: #{html.escape(o.service_id)} | Кол-во: <b>{o.quantity}</b>\n"
            f"  Ссылка: <code>{html.escape(clip(o.link, 30))}</code> | Статус: <i>{html.escape(o.status)}</i>"
            + (f"\n  Refill: {html.escape(o.refill_status)}" if o.refill_status else "")
        )

    b = InlineKeyboardBuilder()
    for order in orders:
        b.row(InlineKeyboardButton(text=f"Заказ #{order.smm_order_id} · статус / refill", callback_data=f"autosmm:order:{order.id}"))
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


@router.callback_query(F.data.startswith("autosmm:order:"))
async def autosmm_order_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    order_id = int(call.data.rsplit(":", 1)[-1])
    try:
        order = await get_owned_order(svc().db, account.id, order_id)
    except SmmError:
        await call.answer("Заказ не найден", show_alert=True)
        return
    await call.answer("Проверяю статус…")
    available, warning = False, ""
    try:
        client, _ = await _get_smm_client(account)
        if not client:
            raise SmmError("Настройте API панели.")
        order, available = await refresh_order(svc().db, account.id, order_id, client)
    except Exception as exc:
        warning = f"\n⚠️ {html.escape(str(exc)[:250])}"
    text = (
        f"📊 <b>Заказ #{html.escape(order.smm_order_id)}</b>\n\n"
        f"Сделка: <code>{html.escape(order.deal_id)}</code>\n"
        f"Услуга: #{html.escape(order.service_id)} · {html.escape(order.service_name)}\n"
        f"Количество: {order.quantity}\n"
        f"Ссылка: <code>{html.escape(order.link)}</code>\n"
        f"Статус: <b>{html.escape(order.status)}</b>\n"
        f"Refill: <b>{html.escape(order.refill_status or 'не запрашивался')}</b>\n"
        f"ID refill: <code>{html.escape(order.refill_id or '—')}</code>" + warning
    )
    rows = []
    if available:
        rows.append([InlineKeyboardButton(text="🔄 Запросить refill", callback_data=f"autosmm:refill_ask:{order.id}")])
    rows += [[InlineKeyboardButton(text="Обновить статус", callback_data=f"autosmm:order:{order.id}")],
             [InlineKeyboardButton(text="К истории", callback_data="autosmm:history:0")]]
    await edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("autosmm:refill_ask:"))
async def autosmm_refill_ask(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    order_id = int(call.data.rsplit(":", 1)[-1])
    try:
        order = await get_owned_order(svc().db, account.id, order_id)
    except SmmError:
        await call.answer("Заказ не найден", show_alert=True)
        return
    legacy = not bool(order.provider_url)
    note = "\n⚠️ Это старый заказ. Подтвердите, что он действительно принадлежит настроенной сейчас панели." if legacy else ""
    await edit(call, f"Запросить refill для заказа #{html.escape(order.smm_order_id)}?{note}\n"
               "Будет отправлен один запрос. Панель проверит доступность.",
               InlineKeyboardMarkup(inline_keyboard=[
                   [InlineKeyboardButton(text="Подтвердить" + (" текущую панель" if legacy else ""),
                                         callback_data=f"autosmm:refill_yes:{order.id}:{int(legacy)}")],
                   [InlineKeyboardButton(text="Отмена", callback_data=f"autosmm:order:{order.id}")],
               ]))


@router.callback_query(F.data.startswith("autosmm:refill_yes:"))
async def autosmm_refill_confirm(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    _, _, raw_id, legacy = call.data.split(":")
    order_id = int(raw_id)
    await call.answer("Отправляю запрос…")
    try:
        client, _ = await _get_smm_client(account)
        if not client:
            raise SmmError("Настройте API панели.")
        order = await request_order_refill(svc().db, account.id, order_id, client, confirm_legacy=legacy == "1")
        text = f"✅ Refill принят: <code>#{html.escape(order.refill_id or '—')}</code>. Статус: {html.escape(order.refill_status or '—')}."
    except Exception as exc:
        text = f"⚠️ {html.escape(str(exc)[:250])}"
    await edit(call, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="К заказу / проверить статус", callback_data=f"autosmm:order:{order_id}")],
        [InlineKeyboardButton(text="К истории", callback_data="autosmm:history:0")],
    ]))
