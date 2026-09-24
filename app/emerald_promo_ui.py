from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select

from .crypto import SecretCipher
from .emerald_promo_manager import (
    ACTIVATION_URL,
    DEFAULT_API_URL,
    EmeraldClient,
    EmeraldPromoIssue,
    EmeraldPromoLotRule,
    EmeraldPromoSetting,
    format_tokens,
    free_message,
    review_message,
    sale_message,
    validate_api_url,
    validate_token_amount,
)
from .handlers import clip, edit, require_account, svc
from .keyboards import back_menu
from .models import PlayerokAccount
from .states import EmeraldPromoState


logger = logging.getLogger("emerald_promo_ui")
router = Router(name="emerald_promo")


async def get_or_create_settings(account_id: Any) -> EmeraldPromoSetting:
    async with svc().db() as session:
        setting = await session.scalar(
            select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == account_id)
        )
        if not setting:
            setting = EmeraldPromoSetting(account_id=account_id)
            session.add(setting)
            await session.commit()
            await session.refresh(setting)
        return setting


def get_token(setting: EmeraldPromoSetting, cipher: SecretCipher) -> str:
    if not setting.api_token_enc:
        return ""
    try:
        return cipher.decrypt(setting.api_token_enc) or ""
    except Exception:
        return ""


def token_label(token: str) -> str:
    if not token:
        return "не настроен"
    if len(token) > 10:
        return token[:6] + "..." + token[-4:]
    return "настроен"


async def render_emerald_settings(call: CallbackQuery, account: PlayerokAccount) -> None:
    setting = await get_or_create_settings(account.id)
    cipher = svc().cipher
    token = get_token(setting, cipher)

    async with svc().db() as session:
        rules = list((await session.scalars(
            select(EmeraldPromoLotRule).where(EmeraldPromoLotRule.account_id == account.id)
        )).all())
        total_sent = await session.scalar(
            select(func.count(EmeraldPromoIssue.id)).where(
                EmeraldPromoIssue.account_id == account.id,
                EmeraldPromoIssue.status == "sent",
            )
        ) or 0
        total_failed = await session.scalar(
            select(func.count(EmeraldPromoIssue.id)).where(
                EmeraldPromoIssue.account_id == account.id,
                EmeraldPromoIssue.status == "failed",
            )
        ) or 0

    active_rules = sum(1 for r in rules if r.enabled)
    issue_mode = "🔑 API-ключ (emeraldai.beer)" if setting.issue_type == "api_key" else "🎟 Промокод"
    free_state = "включена" if setting.free_enabled else "выключена"

    lines = [
        "⚙️ <b>Emerald Promo</b>",
        "",
        f"Playerok-аккаунт: <b>{html.escape(account.username)}</b>",
        f"Клиентский портал: <code>{ACTIVATION_URL}</code>",
        f"Base URL API: <code>{html.escape(setting.api_base_url)}</code>",
        f"API-токен продавца: <b>{html.escape(token_label(token))}</b>",
        f"Режим выдачи: <b>{issue_mode}</b>",
        f"Target ключей: <code>{html.escape(setting.key_target)}</code>",
        f"Команда #free: <b>{free_state}</b>",
        f"Бесплатный номинал: <b>{format_tokens(setting.free_token_amount)}</b>",
        f"Мин. возраст Playerok: <b>{setting.min_account_age_days} дн.</b>",
        f"Привязки: <b>{active_rules}/{len(rules)} включено</b>",
        f"Выдано: <b>{total_sent}</b> · ошибок: <b>{total_failed}</b>",
        "",
        "💡 Покупатели могут проверить баланс ключа командой <code>#баланс</code> в чате заказа.",
    ]

    if rules:
        lines.extend(["", "<b>Лоты:</b>"])
        for r in rules[:8]:
            state = "✅" if r.enabled else "⏸"
            bonus = f" · бонус {format_tokens(r.review_bonus_tokens)}" if r.review_bonus_enabled else ""
            lines.append(f"{state} {html.escape(clip(r.lot_title, 40))}\n   {format_tokens(r.tokens_per_unit)} токенов за 1 шт.{bonus}")

    b = InlineKeyboardBuilder()
    mode_text = "📦 Режим: Ключ ➔ Промокод" if setting.issue_type == "api_key" else "📦 Режим: Промокод ➔ Ключ"
    b.row(InlineKeyboardButton(text=mode_text, callback_data="emp:toggle:mode"))
    b.row(InlineKeyboardButton(text="🎯 Target ключа (funpay_shared/auto)", callback_data="emp:set:target"))
    b.row(InlineKeyboardButton(text="🌐 Base URL API", callback_data="emp:set:base"))
    b.row(InlineKeyboardButton(text="🔑 API-токен", callback_data="emp:set:token"))
    b.row(InlineKeyboardButton(text="🎁 Вкл/выкл #free", callback_data="emp:free"))
    b.row(InlineKeyboardButton(text="🪙 Номинал #free", callback_data="emp:set:free"))
    b.row(InlineKeyboardButton(text="📅 Возраст для #free", callback_data="emp:set:age"))
    b.row(InlineKeyboardButton(text="➕ Добавить лот (выбор кнопкой)", callback_data="emp:lots"))
    b.row(InlineKeyboardButton(text="🧩 Управление лотами", callback_data="emp:rules"))
    b.row(InlineKeyboardButton(text="🧪 Проверить API и баланс", callback_data="emp:api"))
    if total_failed > 0:
        b.row(InlineKeyboardButton(text="🔁 Повторить ошибки", callback_data="emp:retry"))
    b.row(
        InlineKeyboardButton(text="🔄 Обновить", callback_data="emp:open"),
        InlineKeyboardButton(text="⬅️ Плагины", callback_data="plugins:mine"),
    )

    await edit(call, "\n".join(lines), b.as_markup())


@router.callback_query(F.data == "emp:open")
async def emp_open(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    await render_emerald_settings(call, account)


@router.callback_query(F.data == "emp:toggle:mode")
async def emp_toggle_mode(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        setting = await session.scalar(select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == account.id))
        if setting:
            setting.issue_type = "promo" if setting.issue_type == "api_key" else "api_key"
            await session.commit()
    await call.answer("Режим переключен")
    await render_emerald_settings(call, account)


@router.callback_query(F.data == "emp:free")
async def emp_toggle_free(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        setting = await session.scalar(select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == account.id))
        if setting:
            setting.free_enabled = not setting.free_enabled
            await session.commit()
    await call.answer("Сохранено")
    await render_emerald_settings(call, account)


@router.callback_query(F.data.startswith("emp:set:"))
async def emp_set_param(call: CallbackQuery, state: FSMContext) -> None:
    field = call.data.split(":", 2)[2]
    await call.answer()
    await state.set_state(EmeraldPromoState.input_value)
    await state.update_data(field=field)

    prompts = {
        "base": f"🌐 <b>Base URL API</b>\n\nОтправьте HTTPS Base URL API. По умолчанию: <code>{DEFAULT_API_URL}</code>",
        "token": "🔑 <b>API-токен продавца</b>\n\nОтправьте Seller API-токен (начинается с <code>sk-em-seller-</code>). Сообщение будет удалено, а токен сохранён зашифрованным.",
        "target": "🎯 <b>Target ключа</b>\n\nОтправьте target для создания API-ключей (обычно <code>funpay_shared</code> для emeraldai.beer, либо <code>auto</code>, <code>public</code>).",
        "free": "🪙 <b>Номинал #free</b>\n\nОтправьте номинал #free от 10 000 до 1 000 000 000 токенов (по умолчанию 200 000).",
        "age": "📅 <b>Возраст аккаунта</b>\n\nОтправьте минимальный возраст аккаунта в днях (0–3650). Рекомендуется 7.",
    }
    b = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="emp:open")]])
    await edit(call, prompts.get(field, "Введите значение:"), b)


@router.callback_query(F.data == "emp:lots")
async def emp_lots(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer("Загружаю товары…")
    try:
        client = await svc().gateway.get_client(account)
        page = await client.call("get_my_items", statuses=None, count=40)
        items = list(getattr(page, "items", []) or [])
    except Exception as exc:
        await edit(call, f"❌ Ошибка загрузки товаров: <code>{html.escape(str(exc))[:600]}</code>", back_menu("plugins"))
        return

    async with svc().db() as session:
        existing = set((await session.scalars(
            select(EmeraldPromoLotRule.lot_id).where(EmeraldPromoLotRule.account_id == account.id)
        )).all())

    available = [item for item in items if str(getattr(item, "id", "")) not in existing]
    if not available:
        await edit(
            call,
            "❌ <b>Свободных товаров не найдено</b>\n\nВсе ваши выставленные товары уже привязаны к Emerald Promo либо список пуст.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="emp:open")]]),
        )
        return

    b = InlineKeyboardBuilder()
    for item in available[:30]:
        name = getattr(item, "name", "Товар") or "Товар"
        iid = str(getattr(item, "id", ""))
        price = getattr(item, "price", None)
        price_str = f" ({price}₽)" if price is not None else ""
        b.button(
            text=f"📦 {clip(name, 28)}{price_str}",
            callback_data=f"emp:lot:{iid}",
        )
    b.button(text="⬅️ Emerald Promo", callback_data="emp:open")
    b.adjust(1)

    await edit(
        call,
        "🛒 <b>Выберите товар кнопкой:</b>\n\n"
        "После выбора лота бот запросит количество токенов за 1 штуку. Никаких JSON писать не нужно!",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("emp:lot:"))
async def emp_lot_selected(call: CallbackQuery, state: FSMContext) -> None:
    lot_id = call.data.split(":", 2)[2]
    account = await require_account(call)
    if not account:
        return
    await call.answer()

    # Find item title
    title = f"Лот {lot_id}"
    try:
        client = await svc().gateway.get_client(account)
        page = await client.call("get_my_items", statuses=None, count=40)
        for it in list(getattr(page, "items", []) or []):
            if str(getattr(it, "id", "")) == lot_id:
                title = getattr(it, "name", title) or title
                break
    except Exception:
        pass

    await state.set_state(EmeraldPromoState.input_value)
    await state.update_data(field="new_tokens", lot_id=lot_id, lot_title=title)

    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="emp:lots")]])
    await edit(
        call,
        f"🛒 <b>Выбранный лот:</b>\n<b>{html.escape(title)}</b>\nID: <code>{html.escape(lot_id)}</code>\n\n"
        "Теперь отправьте количество токенов за одну купленную единицу (например: <code>100000</code> или <code>50 000</code>):",
        markup,
    )


@router.callback_query(F.data == "emp:rules")
async def emp_rules(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    async with svc().db() as session:
        rules = list((await session.scalars(
            select(EmeraldPromoLotRule).where(EmeraldPromoLotRule.account_id == account.id).order_by(EmeraldPromoLotRule.created_at.desc())
        )).all())

    b = InlineKeyboardBuilder()
    for rule in rules:
        state_icon = "✅" if rule.enabled else "⏸"
        b.button(
            text=f"{state_icon} {clip(rule.lot_title, 26)} · {format_tokens(rule.tokens_per_unit)}",
            callback_data=f"emp:r:{rule.id}",
        )
    b.button(text="➕ Добавить лот (выбор кнопкой)", callback_data="emp:lots")
    b.button(text="⬅️ Настройки", callback_data="emp:open")
    b.adjust(1)

    text = "🧩 <b>Привязки товаров Emerald Promo</b>\n\nВыберите лот для настройки или удаления:" if rules else "🧩 Привязок пока нет. Нажмите «Добавить лот» для выбора товара кнопкой."
    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("emp:r:"))
async def emp_rule_detail(call: CallbackQuery) -> None:
    rule_id = int(call.data.split(":", 2)[2])
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    async with svc().db() as session:
        rule = await session.get(EmeraldPromoLotRule, rule_id)
        if not rule or rule.account_id != account.id:
            await call.answer("Привязка не найдена", show_alert=True)
            return

    text = (
        f"🛒 <b>{html.escape(rule.lot_title)}</b>\n\n"
        f"ID лота: <code>{html.escape(rule.lot_id)}</code>\n"
        f"За 1 шт.: <b>{format_tokens(rule.tokens_per_unit)}</b> токенов\n"
        f"Бонус за 5★: <b>{'включён' if rule.review_bonus_enabled else 'выключен'}</b>\n"
        f"Номинал бонуса: <b>{format_tokens(rule.review_bonus_tokens)}</b> токенов\n"
        f"Статус: <b>{'включён' if rule.enabled else 'выключен'}</b>"
    )

    bonus_btn = "🚫 Выключить бонус 5★" if rule.review_bonus_enabled else "⭐ Включить бонус 5★"
    toggle_btn = "⏸ Выключить лот" if rule.enabled else "▶️ Включить лот"

    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🪙 Изменить номинал", callback_data=f"emp:rt:{rule_id}"))
    b.row(InlineKeyboardButton(text=bonus_btn, callback_data=f"emp:rb:{rule_id}"))
    b.row(InlineKeyboardButton(text="🎁 Изменить бонус", callback_data=f"emp:rbt:{rule_id}"))
    b.row(InlineKeyboardButton(text=toggle_btn, callback_data=f"emp:re:{rule_id}"))
    b.row(InlineKeyboardButton(text="🗑 Удалить привязку", callback_data=f"emp:rd:{rule_id}"))
    b.row(InlineKeyboardButton(text="⬅️ Все лоты", callback_data="emp:rules"))

    await edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("emp:rb:"))
async def emp_rule_toggle_bonus(call: CallbackQuery) -> None:
    rule_id = int(call.data.split(":", 2)[2])
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        rule = await session.get(EmeraldPromoLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.review_bonus_enabled = not rule.review_bonus_enabled
            await session.commit()
    await call.answer("Сохранено")
    await emp_rule_detail(call)


@router.callback_query(F.data.startswith("emp:re:"))
async def emp_rule_toggle_enabled(call: CallbackQuery) -> None:
    rule_id = int(call.data.split(":", 2)[2])
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        rule = await session.get(EmeraldPromoLotRule, rule_id)
        if rule and rule.account_id == account.id:
            rule.enabled = not rule.enabled
            await session.commit()
    await call.answer("Сохранено")
    await emp_rule_detail(call)


@router.callback_query(F.data.startswith("emp:rd:"))
async def emp_rule_delete_confirm(call: CallbackQuery) -> None:
    rule_id = int(call.data.split(":", 2)[2])
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, удалить", callback_data=f"emp:rx:{rule_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"emp:r:{rule_id}")],
    ])
    await edit(call, "Удалить эту привязку лота? Уже выданные ключи и промокоды сохранятся.", markup)


@router.callback_query(F.data.startswith("emp:rx:"))
async def emp_rule_delete_exec(call: CallbackQuery) -> None:
    rule_id = int(call.data.split(":", 2)[2])
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        rule = await session.get(EmeraldPromoLotRule, rule_id)
        if rule and rule.account_id == account.id:
            await session.delete(rule)
            await session.commit()
    await call.answer("Привязка удалена", show_alert=True)
    await emp_rules(call)


@router.callback_query(F.data.startswith("emp:rt:"))
async def emp_rule_edit_tokens(call: CallbackQuery, state: FSMContext) -> None:
    rule_id = int(call.data.split(":", 2)[2])
    await call.answer()
    await state.set_state(EmeraldPromoState.input_value)
    await state.update_data(field="edit_tokens", rule_id=rule_id)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"emp:r:{rule_id}")]])
    await edit(call, "🪙 <b>Изменение номинала</b>\n\nОтправьте новый номинал токенов за 1 штуку (минимум 10 000):", markup)


@router.callback_query(F.data.startswith("emp:rbt:"))
async def emp_rule_edit_bonus(call: CallbackQuery, state: FSMContext) -> None:
    rule_id = int(call.data.split(":", 2)[2])
    await call.answer()
    await state.set_state(EmeraldPromoState.input_value)
    await state.update_data(field="edit_bonus", rule_id=rule_id)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"emp:r:{rule_id}")]])
    await edit(call, "🎁 <b>Изменение бонуса</b>\n\nОтправьте номинал бонусных токенов за отзыв 5★ (минимум 10 000):", markup)


@router.callback_query(F.data == "emp:api")
async def emp_api_test(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer("Проверяю API…")
    setting = await get_or_create_settings(account.id)
    token = get_token(setting, svc().cipher)
    if not token:
        await edit(call, "❌ <b>API-токен не настроен</b>\n\nСначала укажите Seller токен в настройках.", back_menu("emp:open"))
        return

    try:
        client = EmeraldClient(setting.api_base_url, token)
        data = await asyncio.to_thread(client.get_account)
        bal = int(data.get("balance_tokens") or 0)
        email = str(data.get("email") or "—")
        pricing = data.get("pricing", {})
        min_promo = pricing.get("minimum_token_promo", 10000)

        text = (
            "✅ <b>Emerald API отвечает!</b>\n\n"
            f"Email аккаунта: <code>{html.escape(email)}</code>\n"
            f"Баланс токенов: <b>{format_tokens(bal)}</b>\n"
            f"Минимум промокода: <b>{format_tokens(min_promo)}</b>\n"
            f"Base URL: <code>{html.escape(setting.api_base_url)}</code>"
        )
    except Exception as exc:
        text = f"❌ <b>Ошибка подключения к Emerald API:</b>\n\n<code>{html.escape(str(exc))}</code>"

    await edit(call, text, InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="emp:open")]]))


@router.message(EmeraldPromoState.input_value, F.text)
async def emp_handle_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("field")
    account = await svc().db()
    async with account as session:
        user_row = await session.scalar(select(PlayerokAccount).where(PlayerokAccount.tg_user_id == message.from_user.id))
    if not user_row:
        await state.clear()
        return

    raw_val = (message.text or "").strip()
    setting = await get_or_create_settings(user_row.id)

    try:
        if field == "token":
            try:
                await message.delete()
            except Exception:
                pass
            if not raw_val.startswith("sk-em-seller-") or len(raw_val) < 16:
                await message.answer("❌ Токен должен начинаться с <code>sk-em-seller-</code> и быть не короче 16 символов.", parse_mode="HTML")
                return
            enc = svc().cipher.encrypt(raw_val)
            async with svc().db() as session:
                st = await session.scalar(select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == user_row.id))
                st.api_token_enc = enc
                await session.commit()
            await state.clear()
            await message.answer("✅ API-токен сохранён в зашифрованном виде.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ К настройкам", callback_data="emp:open")]]))
            return

        elif field == "base":
            url = validate_api_url(raw_val)
            async with svc().db() as session:
                st = await session.scalar(select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == user_row.id))
                st.api_base_url = url
                await session.commit()
            await state.clear()
            await message.answer(f"✅ Base URL установлен: <code>{html.escape(url)}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ К настройкам", callback_data="emp:open")]]))
            return

        elif field == "target":
            target = raw_val.lower()
            if target not in {"funpay_shared", "auto", "public", "seller_site"}:
                await message.answer("Target должен быть одним из: <code>funpay_shared</code>, <code>auto</code>, <code>public</code>, <code>seller_site</code>", parse_mode="HTML")
                return
            async with svc().db() as session:
                st = await session.scalar(select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == user_row.id))
                st.key_target = target
                await session.commit()
            await state.clear()
            await message.answer(f"✅ Target ключей установлен: <code>{html.escape(target)}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ К настройкам", callback_data="emp:open")]]))
            return

        elif field == "free":
            val = validate_token_amount(raw_val)
            async with svc().db() as session:
                st = await session.scalar(select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == user_row.id))
                st.free_token_amount = val
                await session.commit()
            await state.clear()
            await message.answer(f"✅ Номинал #free установлен: <b>{format_tokens(val)}</b> токенов.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ К настройкам", callback_data="emp:open")]]))
            return

        elif field == "age":
            days = int(raw_val)
            if not 0 <= days <= 3650:
                raise ValueError("Возраст от 0 до 3650 дней.")
            async with svc().db() as session:
                st = await session.scalar(select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == user_row.id))
                st.min_account_age_days = days
                await session.commit()
            await state.clear()
            await message.answer(f"✅ Мин. возраст аккаунта: <b>{days}</b> дн.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ К настройкам", callback_data="emp:open")]]))
            return

        elif field == "new_tokens":
            lot_id = data.get("lot_id")
            lot_title = data.get("lot_title")
            val = validate_token_amount(raw_val)
            async with svc().db() as session:
                rule = await session.scalar(
                    select(EmeraldPromoLotRule).where(
                        EmeraldPromoLotRule.account_id == user_row.id,
                        EmeraldPromoLotRule.lot_id == lot_id,
                    )
                )
                if not rule:
                    rule = EmeraldPromoLotRule(
                        account_id=user_row.id,
                        lot_id=lot_id,
                        lot_title=lot_title,
                        tokens_per_unit=val,
                    )
                    session.add(rule)
                else:
                    rule.tokens_per_unit = val
                    rule.lot_title = lot_title
                await session.commit()
            await state.clear()
            await message.answer(
                f"✅ <b>Привязка создана успешно!</b>\n\n"
                f"Лот: <b>{html.escape(lot_title)}</b>\n"
                f"Номинал: <b>{format_tokens(val)}</b> токенов за 1 шт.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧩 Управление лотами", callback_data="emp:rules")]]),
            )
            return

        elif field == "edit_tokens":
            rule_id = data.get("rule_id")
            val = validate_token_amount(raw_val)
            async with svc().db() as session:
                rule = await session.get(EmeraldPromoLotRule, rule_id)
                if rule and rule.account_id == user_row.id:
                    rule.tokens_per_unit = val
                    await session.commit()
            await state.clear()
            await message.answer(f"✅ Номинал обновлён: <b>{format_tokens(val)}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К лоту", callback_data=f"emp:r:{rule_id}")]]))
            return

        elif field == "edit_bonus":
            rule_id = data.get("rule_id")
            val = validate_token_amount(raw_val)
            async with svc().db() as session:
                rule = await session.get(EmeraldPromoLotRule, rule_id)
                if rule and rule.account_id == user_row.id:
                    rule.review_bonus_tokens = val
                    await session.commit()
            await state.clear()
            await message.answer(f"✅ Бонус обновлён: <b>{format_tokens(val)}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К лоту", callback_data=f"emp:r:{rule_id}")]]))
            return

    except Exception as exc:
        await message.answer(f"❌ Ошибка: {str(exc)}")
