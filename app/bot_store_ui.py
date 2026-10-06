from __future__ import annotations

import html
import logging
from typing import Any
import httpx

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, select

from .bot_store_manager import (
    BOT_TYPES,
    DEFAULT_API_URL,
    GITHUB_SOURCES_URL,
    BotStoreConfig,
    BotStoreDeal,
    BotStoreLotRule,
)
from .handlers import active_account, clip, edit, require_account, svc
from .keyboards import back_menu
from .models import PlayerokAccount

logger = logging.getLogger("bot_store_ui")
router = Router(name="bot_store_ui")


class BotStoreState(StatesGroup):
    waiting_api_url = State()
    waiting_api_key = State()
    choosing_lot = State()
    choosing_bot_type = State()


async def get_or_create_config(account_id: Any) -> BotStoreConfig:
    async with svc().db() as session:
        cfg = await session.scalar(
            select(BotStoreConfig).where(BotStoreConfig.account_id == account_id)
        )
        if not cfg:
            cfg = BotStoreConfig(account_id=account_id)
            session.add(cfg)
            await session.commit()
            await session.refresh(cfg)
        return cfg


@router.callback_query(F.data == "botstore:open")
async def botstore_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    cfg = await get_or_create_config(account.id)
    has_api_key = bool(cfg.api_key_enc)

    async with svc().db() as session:
        rules = (
            await session.scalars(
                select(BotStoreLotRule)
                .where(BotStoreLotRule.account_id == account.id)
                .order_by(BotStoreLotRule.id.desc())
            )
        ).all()

    rules_count = len(rules)

    text = (
        "🤖 <b>Панель Автохостинга и Выдачи Ботов</b>\n\n"
        f"🌐 <b>API URL:</b> <code>{html.escape(cfg.api_url or DEFAULT_API_URL)}</code>\n"
        f"🔑 <b>API Key:</b> {'✅ Настроен' if has_api_key else '❌ Не настроен'}\n"
        f"📦 <b>Сурсы (GitHub):</b> <code>{html.escape(GITHUB_SOURCES_URL)}</code>\n"
        f"📑 <b>Привязанных лотов:</b> {rules_count}\n\n"
        "После оплаты лота покупатель получает сурсы и выбор:\n"
        "• <b>#да</b> — бот запросит Bot Token и Admin ID, затем развернет бота через API хостинга.\n"
        "• <b>#нет</b> — покупатель забирает только сурсы, сделка переводится в «Выполнен»."
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔑 Настроить API Key", callback_data="botstore:set_key"),
        InlineKeyboardButton(text="🌐 Сменить API URL", callback_data="botstore:set_url"),
    )
    builder.row(
        InlineKeyboardButton(text="📑 Привязанные лоты", callback_data="botstore:rules:list"),
        InlineKeyboardButton(text="➕ Привязать лот", callback_data="botstore:rules:add"),
    )
    builder.row(
        InlineKeyboardButton(text="🔌 Проверить подключение к API", callback_data="botstore:test_api"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад к плагинам", callback_data="plugins:mine"),
    )

    await call.answer()
    await edit(call, text, builder.as_markup())


@router.callback_query(F.data == "botstore:set_key")
async def botstore_set_key_start(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.set_state(BotStoreState.waiting_api_key)
    await call.answer()
    await edit(
        call,
        "🔑 <b>Введите ваш Bearer API Key от хостинга:</b>\n\n"
        "Получите его на сайте (напр. <code>eh_live_...</code>).\n"
        "Отправьте ключ сообщением или нажмите «Отмена».",
        back_menu("botstore:open"),
    )


@router.message(BotStoreState.waiting_api_key)
async def botstore_set_key_save(message: Message, state: FSMContext) -> None:
    key_text = (message.text or "").strip()
    if not key_text:
        await message.reply("❌ Ключ не может быть пустым.")
        return

    account = await active_account(message.from_user.id)
    if not account:
        return

    key_enc = svc().cipher.encrypt(key_text)
    async with svc().db() as session:
        cfg = await session.scalar(
            select(BotStoreConfig).where(BotStoreConfig.account_id == account.id)
        )
        if not cfg:
            cfg = BotStoreConfig(account_id=account.id, api_key_enc=key_enc)
            session.add(cfg)
        else:
            cfg.api_key_enc = key_enc
        await session.commit()

    await state.clear()
    await message.reply(
        "✅ <b>API Key успешно сохранен и зашифрован!</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню плагина", callback_data="botstore:open")]]
        ),
    )


@router.callback_query(F.data == "botstore:set_url")
async def botstore_set_url_start(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.set_state(BotStoreState.waiting_api_url)
    await call.answer()
    await edit(
        call,
        f"🌐 <b>Введите базовый URL хостинга:</b>\n\n"
        f"По умолчанию: <code>{DEFAULT_API_URL}</code>\n"
        "Отправьте новый URL сообщением:",
        back_menu("botstore:open"),
    )


@router.message(BotStoreState.waiting_api_url)
async def botstore_set_url_save(message: Message, state: FSMContext) -> None:
    url_text = (message.text or "").strip().rstrip("/")
    if not url_text.startswith("http"):
        await message.reply("❌ URL должен начинаться с http:// или https://")
        return

    account = await active_account(message.from_user.id)
    if not account:
        return

    async with svc().db() as session:
        cfg = await session.scalar(
            select(BotStoreConfig).where(BotStoreConfig.account_id == account.id)
        )
        if not cfg:
            cfg = BotStoreConfig(account_id=account.id, api_url=url_text)
            session.add(cfg)
        else:
            cfg.api_url = url_text
        await session.commit()

    await state.clear()
    await message.reply(
        f"✅ <b>API URL обновлен на:</b> <code>{html.escape(url_text)}</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню плагина", callback_data="botstore:open")]]
        ),
    )


@router.callback_query(F.data == "botstore:test_api")
async def botstore_test_api(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    cfg = await get_or_create_config(account.id)
    if not cfg.api_key_enc:
        await call.answer("❌ Сначала укажите API Key!", show_alert=True)
        return

    key = svc().cipher.decrypt(cfg.api_key_enc)
    api_url = (cfg.api_url or DEFAULT_API_URL).rstrip("/")

    await call.answer("Проверяем связь с API...")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{api_url}/v1/me", headers={"Authorization": f"Bearer {key}"})
            if resp.status_code == 200:
                data = resp.json()
                email = data.get("user", {}).get("email", "unknown")
                await call.message.reply(
                    f"✅ <b>Связь с API хостинга успешна!</b>\n\n"
                    f"🌐 Сервер: <code>{html.escape(api_url)}</code>\n"
                    f"👤 Аккаунт: <code>{html.escape(email)}</code>\n"
                    f"🔑 Ключ: {html.escape(data.get('api_key', {}).get('name', 'ok'))}"
                )
            else:
                await call.message.reply(
                    f"⚠️ Ошибка API (HTTP {resp.status_code}):\n<code>{html.escape(resp.text[:300])}</code>"
                )
    except Exception as exc:
        await call.message.reply(f"❌ Не удалось подключиться к API:\n<code>{html.escape(str(exc))}</code>")


# --- Управление привязкой лотов ---

@router.callback_query(F.data == "botstore:rules:list")
async def botstore_rules_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        rules = (
            await session.scalars(
                select(BotStoreLotRule)
                .where(BotStoreLotRule.account_id == account.id)
                .order_by(BotStoreLotRule.id.desc())
            )
        ).all()

    if not rules:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="➕ Привязать лот", callback_data="botstore:rules:add"))
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="botstore:open"))
        await call.answer()
        await edit(call, "ℹ️ У вас пока нет привязанных лотов для автохостинга ботов.", builder.as_markup())
        return

    builder = InlineKeyboardBuilder()
    for r in rules:
        status_emoji = "🟢" if r.enabled else "🔴"
        b_type_name = "Бот-Магазин + Mini App"
        builder.row(
            InlineKeyboardButton(
                text=f"{status_emoji} {clip(r.lot_title, 24)} ({b_type_name})",
                callback_data=f"botstore:rule:toggle:{r.id}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"botstore:rule:del:{r.id}"),
        )
    builder.row(InlineKeyboardButton(text="➕ Привязать лот", callback_data="botstore:rules:add"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="botstore:open"))

    await call.answer()
    await edit(
        call,
        "📑 <b>Привязанные лоты для автохостинга:</b>\n\n"
        "Нажмите на название чтобы включить/выключить, или на 🗑 чтобы удалить привязку.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("botstore:rule:toggle:"))
async def botstore_rule_toggle(call: CallbackQuery) -> None:
    rule_id = int(call.data.split(":")[3])
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        rule = await session.scalar(
            select(BotStoreLotRule).where(
                BotStoreLotRule.id == rule_id, BotStoreLotRule.account_id == account.id
            )
        )
        if rule:
            rule.enabled = not rule.enabled
            await session.commit()
    await botstore_rules_list(call)


@router.callback_query(F.data.startswith("botstore:rule:del:"))
async def botstore_rule_del(call: CallbackQuery) -> None:
    rule_id = int(call.data.split(":")[3])
    account = await require_account(call)
    if not account:
        return

    async with svc().db() as session:
        await session.execute(
            delete(BotStoreLotRule).where(
                BotStoreLotRule.id == rule_id, BotStoreLotRule.account_id == account.id
            )
        )
        await session.commit()
    await call.answer("Логика привязки удалена")
    await botstore_rules_list(call)


@router.callback_query(F.data == "botstore:rules:add")
async def botstore_rules_add_start(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return

    await call.answer("Загружаем список лотов из Playerok...")
    try:
        items = await svc().gateway.list_user_items(account)
    except Exception as exc:
        await call.message.reply(f"❌ Ошибка загрузки лотов Playerok: {exc}")
        return

    if not items:
        await call.message.reply("❌ В вашем профиле Playerok не найдено активных лотов.")
        return

    builder = InlineKeyboardBuilder()
    for it in items[:40]:
        item_id = str(it.id)
        title = clip(str(it.name or item_id), 30)
        builder.row(InlineKeyboardButton(text=f"📦 {title}", callback_data=f"botstore:choose_item:{item_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="botstore:open"))

    await edit(
        call,
        "📦 <b>Выберите лот Playerok, при оплате которого будет выдаваться бот и предлагаться хостинг:</b>",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("botstore:choose_item:"))
async def botstore_rules_choose_item(call: CallbackQuery, state: FSMContext) -> None:
    lot_id = call.data.split(":")[2]
    account = await require_account(call)
    if not account:
        return

    # Запрашиваем информацию о товаре
    try:
        items = await svc().gateway.list_user_items(account)
        found = next((i for i in items if str(i.id) == lot_id), None)
        lot_title = str(found.name if found else f"Лот #{lot_id}")
    except Exception:
        lot_title = f"Лот #{lot_id}"

    await state.update_data(chosen_lot_id=lot_id, chosen_lot_title=lot_title)

    builder = InlineKeyboardBuilder()
    for b_code, b_title in BOT_TYPES:
        builder.row(InlineKeyboardButton(text=b_title, callback_data=f"botstore:pick_type:{b_code}"))
    builder.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="botstore:open"))

    await call.answer()
    await edit(
        call,
        f"🎯 Выбран лот: <b>{html.escape(lot_title)}</b>\n\n"
        "Выберите тип бота, который будет выдаваться и хоститься:",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("botstore:pick_type:"))
async def botstore_rules_pick_type(call: CallbackQuery, state: FSMContext) -> None:
    bot_type = call.data.split(":")[2]
    account = await require_account(call)
    if not account:
        return

    data = await state.get_data()
    lot_id = data.get("chosen_lot_id")
    lot_title = data.get("chosen_lot_title", f"Лот #{lot_id}")

    if not lot_id:
        await call.answer("Сессия истекла, начните заново", show_alert=True)
        return await botstore_main_menu(call, state)

    async with svc().db() as session:
        existing = await session.scalar(
            select(BotStoreLotRule).where(
                BotStoreLotRule.account_id == account.id, BotStoreLotRule.lot_id == lot_id
            )
        )
        if existing:
            existing.bot_type = bot_type
            existing.lot_title = lot_title
            existing.enabled = True
        else:
            rule = BotStoreLotRule(
                account_id=account.id,
                lot_id=lot_id,
                lot_title=lot_title,
                bot_type=bot_type,
                enabled=True,
            )
            session.add(rule)
        await session.commit()

    await state.clear()
    await call.answer("Лот успешно привязан!")
    await botstore_rules_list(call)
