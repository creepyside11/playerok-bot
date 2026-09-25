from __future__ import annotations

import base64
import copy
import hashlib
import html
import io
import secrets
import uuid
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from playerokapi.enums import GameCategoryDataFieldTypes

from .crypto import SecretCipher
from .keyboards import auth_method_menu, back_menu, delivery_mode_menu, main_menu
from .models import AutoReplyRule, DeliveryRule, DeliveryStock, ItemTemplate, PlayerokAccount, TelegramUser, fresh_settings
from .playerok import DEFAULT_USER_AGENT, EmailAuthClient, PlayerokGateway, normalize_proxy
from .states import AddAccount, AutoReplyAdd, DeliveryAdd, ItemCreate


router = Router(name="main")


class Services:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: PlayerokGateway,
        cipher: SecretCipher,
        email_auth: EmailAuthClient,
    ):
        self.db = session_factory
        self.gateway = gateway
        self.cipher = cipher
        self.email_auth = email_auth


services: Services | None = None


def configure_handlers(value: Services) -> Router:
    global services
    services = value
    return router


def svc() -> Services:
    if services is None:
        raise RuntimeError("Handlers are not configured")
    return services


def clip(value: Any, limit: int = 70) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


async def edit(call: CallbackQuery, text: str, markup=None) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=markup, parse_mode="HTML")


async def ensure_user(tg_id: int) -> TelegramUser:
    async with svc().db() as session:
        user = await session.get(TelegramUser, tg_id)
        if user is None:
            user = TelegramUser(id=tg_id)
            session.add(user)
            await session.commit()
        return user


async def active_account(tg_id: int) -> PlayerokAccount | None:
    await ensure_user(tg_id)
    async with svc().db() as session:
        user = await session.get(TelegramUser, tg_id)
        if not user or not user.active_account_id:
            return None
        return await session.get(PlayerokAccount, user.active_account_id)


async def require_account(call: CallbackQuery) -> PlayerokAccount | None:
    account = await active_account(call.from_user.id)
    if account is None:
        await call.answer("Сначала добавьте Playerok аккаунт", show_alert=True)
        await show_accounts(call)
    return account


def settings(account: PlayerokAccount) -> dict[str, Any]:
    result = fresh_settings()
    raw = account.settings or {}
    result["auto_confirm"] = bool(raw.get("auto_confirm", False))
    result["notifications"].update(raw.get("notifications") or {})
    return result


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    account = await active_account(message.from_user.id)
    tail = f"\n\nАктивный: <b>{html.escape(account.username)}</b>" if account else "\n\nPlayerok аккаунт не подключён."
    await message.answer(
        "🤖 <b>Playerok Bot</b>\nУправление магазином и автоматизация." + tail,
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())


@router.callback_query(F.data == "flow:cancel")
async def cancel_cb(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    await edit(call, "Главное меню", main_menu())


@router.callback_query(F.data == "menu:main")
async def menu_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await active_account(call.from_user.id)
    tail = f"\nАктивный: <b>{html.escape(account.username)}</b>" if account else "\nАккаунт не подключён"
    await call.answer()
    await edit(call, "🤖 <b>Playerok Bot</b>" + tail, main_menu())


async def show_accounts(call: CallbackQuery) -> None:
    await ensure_user(call.from_user.id)
    async with svc().db() as session:
        user = await session.get(TelegramUser, call.from_user.id)
        accounts = list((await session.scalars(
            select(PlayerokAccount)
            .where(PlayerokAccount.tg_user_id == call.from_user.id)
            .order_by(PlayerokAccount.created_at)
        )).all())
    b = InlineKeyboardBuilder()
    for account in accounts:
        active = "✅ " if user and user.active_account_id == account.id else ""
        b.button(text=active + clip(account.username, 25), callback_data=f"account:select:{account.id}")
    b.button(text="➕ Добавить аккаунт", callback_data="account:add")
    if accounts and user and user.active_account_id:
        b.button(text="🗑 Удалить активный", callback_data="account:delete")
    b.button(text="🌐 Доступ к веб-панели", callback_data="menu:web")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    await edit(call, "🔐 <b>Playerok аккаунты</b>", b.as_markup())


@router.callback_query(F.data == "menu:web")
async def web_credentials_menu(call: CallbackQuery) -> None:
    user = await ensure_user(call.from_user.id)
    plain_password = None
    async with svc().db() as session:
        db_user = await session.get(TelegramUser, call.from_user.id)
        if not db_user.web_login or not db_user.web_password_hash:
            db_user.web_login = f"user_{call.from_user.id}"
            plain_password = secrets.token_hex(4)
            db_user.web_password_hash = hashlib.sha256(plain_password.encode()).hexdigest()
            await session.commit()
            login = db_user.web_login
            pwd_text = f"<code>{plain_password}</code> (сохраните сейчас)"
        else:
            login = db_user.web_login
            pwd_text = "•••••••• (уже сгенерирован)"

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Сгенерировать новый пароль", callback_data="web:reset_password")
    b.button(text="⬅️ Назад", callback_data="menu:accounts")
    b.adjust(1)

    text = (
        "🌐 <b>Доступ к Веб-панели управления</b>\n\n"
        f"👤 Логин: <code>{html.escape(login)}</code>\n"
        f"🔑 Пароль: {pwd_text}\n\n"
        "<i>Используйте эти данные для входа в веб-версию Playerok BOT. Все настройки синхронизируются в реальном времени через общую базу данных.</i>"
    )
    await edit(call, text, b.as_markup())


@router.callback_query(F.data == "web:reset_password")
async def web_reset_password(call: CallbackQuery) -> None:
    plain_password = secrets.token_hex(4)
    async with svc().db() as session:
        db_user = await session.get(TelegramUser, call.from_user.id)
        if db_user:
            if not db_user.web_login:
                db_user.web_login = f"user_{call.from_user.id}"
            db_user.web_password_hash = hashlib.sha256(plain_password.encode()).hexdigest()
            await session.commit()
            login = db_user.web_login
        else:
            login = f"user_{call.from_user.id}"

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Сгенерировать снова", callback_data="web:reset_password")
    b.button(text="⬅️ Назад", callback_data="menu:accounts")
    b.adjust(1)

    text = (
        "✅ <b>Новый пароль для веб-панели сгенерирован!</b>\n\n"
        f"👤 Логин: <code>{html.escape(login)}</code>\n"
        f"🔑 Новый пароль: <code>{plain_password}</code>\n\n"
        "⚠️ Скопируйте и сохраните пароль, он отображается в открытом виде только один раз."
    )
    await call.answer("Пароль обновлен")
    await edit(call, text, b.as_markup())


@router.callback_query(F.data == "menu:accounts")
async def accounts_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show_accounts(call)


@router.callback_query(F.data.startswith("account:select:"))
async def account_select(call: CallbackQuery) -> None:
    try:
        account_id = uuid.UUID(call.data.rsplit(":", 1)[-1])
    except ValueError:
        await call.answer("Некорректный ID", show_alert=True)
        return
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, account_id)
        user = await session.get(TelegramUser, call.from_user.id)
        if not account or account.tg_user_id != call.from_user.id or not user:
            await call.answer("Аккаунт не найден", show_alert=True)
            return
        user.active_account_id = account.id
        await session.commit()
    await call.answer(f"Активен: {account.username}")
    await show_accounts(call)


@router.callback_query(F.data == "account:delete")
async def account_delete(call: CallbackQuery) -> None:
    account = await active_account(call.from_user.id)
    if not account:
        await call.answer("Нет активного аккаунта", show_alert=True)
        return
    async with svc().db() as session:
        user = await session.get(TelegramUser, call.from_user.id)
        row = await session.get(PlayerokAccount, account.id)
        if row:
            await session.delete(row)
        if user:
            user.active_account_id = None
        await session.commit()
    await svc().gateway.invalidate(account.id)
    await call.answer("Удалено")
    await show_accounts(call)


@router.callback_query(F.data == "account:add")
async def account_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddAccount.proxy)
    await call.answer()
    await edit(
        call,
        "🌐 <b>Шаг 1/3 — прокси</b>\n\n"
        "Отправьте <code>ip:port</code>, <code>user:pass@ip:port</code> "
        "или <code>ip:port:user:pass</code>.\n"
        "Отправьте <code>-</code>, чтобы работать без прокси.",
        back_menu("accounts"),
    )


@router.message(AddAccount.proxy)
async def account_proxy(message: Message, state: FSMContext) -> None:
    await state.update_data(
        proxy=normalize_proxy(message.text or ""),
        user_agent=DEFAULT_USER_AGENT,
    )
    await state.set_state(AddAccount.auth_method)
    await message.answer(
        "🔐 <b>Шаг 2/3 — способ входа</b>",
        reply_markup=auth_method_menu(),
        parse_mode="HTML",
    )


@router.callback_query(AddAccount.auth_method, F.data == "auth:cookie")
async def choose_cookie(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAccount.cookie)
    await call.answer()
    await edit(
        call,
        "🍪 Отправьте cookies строкой, например:\n"
        "<code>token=...; __ddg5_=...</code>\n\n"
        "Сообщение будет удалено после обработки, если Telegram позволит.",
        back_menu("accounts"),
    )


async def save_credentials(
    tg_id: int,
    cookies: str,
    proxy: str | None,
    user_agent: str,
) -> PlayerokAccount:
    snapshot = await svc().gateway.validate_raw(cookies, proxy, user_agent)
    async with svc().db() as session:
        user = await session.get(TelegramUser, tg_id)
        if user is None:
            user = TelegramUser(id=tg_id)
            session.add(user)
            await session.flush()
        account = await session.scalar(select(PlayerokAccount).where(
            PlayerokAccount.tg_user_id == tg_id,
            PlayerokAccount.playerok_user_id == snapshot.id,
        ))
        if account is None:
            account = PlayerokAccount(
                tg_user_id=tg_id,
                playerok_user_id=snapshot.id,
                username=snapshot.username,
                email=snapshot.email,
                cookies_encrypted=svc().cipher.encrypt(cookies),
                proxy_encrypted=svc().cipher.encrypt(proxy) if proxy else None,
                user_agent=user_agent,
                settings=fresh_settings(),
                worker_initialized=False,
            )
            session.add(account)
            await session.flush()
        else:
            account.username = snapshot.username
            account.email = snapshot.email
            account.cookies_encrypted = svc().cipher.encrypt(cookies)
            account.proxy_encrypted = svc().cipher.encrypt(proxy) if proxy else None
            account.user_agent = user_agent
            account.worker_initialized = False
        user.active_account_id = account.id
        await session.commit()
        await session.refresh(account)
    await svc().gateway.invalidate(account.id)
    return account


@router.message(AddAccount.cookie)
async def cookie_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        account = await save_credentials(
            message.from_user.id,
            (message.text or "").strip(),
            data.get("proxy"),
            data.get("user_agent") or DEFAULT_USER_AGENT,
        )
    except Exception as exc:
        await message.answer(
            "❌ Авторизация не удалась:\n"
            f"<code>{html.escape(str(exc))[:1500]}</code>",
            parse_mode="HTML",
        )
        return
    finally:
        try:
            await message.delete()
        except Exception:
            pass
    await state.clear()
    await message.answer(
        f"✅ Подключён <b>{html.escape(account.username)}</b>.",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


@router.callback_query(AddAccount.auth_method, F.data == "auth:email")
async def choose_email(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAccount.email)
    await call.answer()
    await edit(call, "📧 Отправьте email Playerok аккаунта.", back_menu("accounts"))


@router.message(AddAccount.email)
async def email_value(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    data = await state.get_data()
    try:
        cookies = await svc().email_auth.request_code(email, data.get("proxy"))
    except Exception as exc:
        await message.answer(
            "❌ Playerok не отправил код:\n"
            f"<code>{html.escape(str(exc))[:1500]}</code>",
            parse_mode="HTML",
        )
        return
    await state.update_data(email=email, auth_session_cookies=cookies)
    await state.set_state(AddAccount.code)
    await message.answer("✉️ Код отправлен на почту. Введите код из письма Playerok.")


@router.message(AddAccount.code)
async def email_code(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        cookies, _ = await svc().email_auth.verify_code(
            data["email"],
            message.text or "",
            data.get("proxy"),
            data.get("auth_session_cookies") or {},
        )
        account = await save_credentials(
            message.from_user.id,
            cookies,
            data.get("proxy"),
            data.get("user_agent") or DEFAULT_USER_AGENT,
        )
    except Exception as exc:
        await message.answer(
            "❌ Код не подтверждён:\n"
            f"<code>{html.escape(str(exc))[:1500]}</code>",
            parse_mode="HTML",
        )
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    await message.answer(
        f"✅ Вход выполнен: <b>{html.escape(account.username)}</b>.",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "menu:profile")
async def profile(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer("Обновляю…")
    try:
        viewer = await (await svc().gateway.get_client(account)).viewer()
        text = (
            "👤 <b>Профиль Playerok</b>\n"
            f"Ник: <b>{html.escape(viewer.username)}</b>\n"
            f"Email: {html.escape(viewer.email or '—')}\n"
            f"ID: <code>{html.escape(viewer.id)}</code>\n"
            f"Отзывы: <b>{viewer.testimonials if viewer.testimonials is not None else '—'}</b>\n"
            f"Продажи разрешены: <b>{'да' if viewer.can_publish_items else 'нет'}</b>\n"
            f"Блокировка: <b>{'да' if viewer.is_blocked else 'нет'}</b>"
        )
    except Exception as exc:
        text = f"❌ Ошибка профиля:\n<code>{html.escape(str(exc))[:1600]}</code>"
    await edit(call, text, back_menu())


@router.callback_query(F.data == "menu:balance")
async def balance(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer("Проверяю…")
    try:
        viewer = await (await svc().gateway.get_client(account)).viewer()
        value = viewer.balance if viewer.balance is not None else "—"
        text = f"💰 <b>Баланс {html.escape(account.username)}</b>\n\n<b>{html.escape(str(value))} ₽</b>"
    except Exception as exc:
        text = f"❌ Ошибка баланса:\n<code>{html.escape(str(exc))[:1600]}</code>"
    await edit(call, text, back_menu())


async def notifications_markup(account: PlayerokAccount) -> InlineKeyboardMarkup:
    cfg = settings(account)
    labels = {
        "new_message": "Новые сообщения",
        "new_deal": "Новые заказы (оплата)",
        "deal_status": "Статусы заказов (выполнение/подтверждение)",
        "new_review": "Новые отзывы покупателей",
        "errors": "Ошибки API",
    }
    b = InlineKeyboardBuilder()
    for key, label in labels.items():
        enabled = cfg.get("notifications", {}).get(key, True)
        b.button(text=f"{'✅' if enabled else '❌'} {label}", callback_data=f"notify:{key}")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "menu:notifications")
async def notifications(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    await edit(
        call,
        f"🔔 <b>Уведомления — {html.escape(account.username)}</b>",
        await notifications_markup(account),
    )


@router.callback_query(F.data.startswith("notify:"))
async def notify_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    key = call.data.split(":", 1)[1]
    if key not in {"new_message", "new_deal", "deal_status", "new_review", "errors"}:
        return
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        cfg = settings(row)
        if "notifications" not in cfg:
            cfg["notifications"] = {}
        cfg["notifications"][key] = not cfg["notifications"].get(key, True)
        row.settings = copy.deepcopy(cfg)
        await session.commit()
        account.settings = cfg
    await call.answer("Сохранено")
    await edit(call, f"🔔 <b>Уведомления — {html.escape(account.username)}</b>", await notifications_markup(account))


async def render_autoconfirm(call: CallbackQuery, account: PlayerokAccount) -> None:
    enabled = settings(account)["auto_confirm"]
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Включено" if enabled else "❌ Выключено",
            callback_data="autoconfirm:toggle",
        )],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main")],
    ])
    await edit(
        call,
        "✅ <b>Автоподтверждение</b>\n\n"
        "Новые продажи PAID/PENDING автоматически переводятся в SENT. "
        "При пустом складе автовыдачи заказ не подтверждается.",
        markup,
    )


@router.callback_query(F.data == "menu:autoconfirm")
async def autoconfirm(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    await render_autoconfirm(call, account)


@router.callback_query(F.data == "autoconfirm:toggle")
async def autoconfirm_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        cfg = settings(row)
        cfg["auto_confirm"] = not cfg["auto_confirm"]
        row.settings = copy.deepcopy(cfg)
        await session.commit()
        account.settings = cfg
    await call.answer("Сохранено")
    await render_autoconfirm(call, account)


async def show_autoreply(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        rules = list((await session.scalars(
            select(AutoReplyRule)
            .where(AutoReplyRule.account_id == account.id)
            .order_by(AutoReplyRule.id)
        )).all())
    b = InlineKeyboardBuilder()
    for rule in rules:
        label = "все сообщения" if rule.trigger == "*" else clip(rule.trigger, 25)
        b.button(text=f"🗑 {label}", callback_data=f"autoreply:delete:{rule.id}")
    b.button(text="➕ Добавить правило", callback_data="autoreply:add")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    await edit(
        call,
        "💬 <b>Автоответчик</b>\n" + ("Нажмите правило, чтобы удалить." if rules else "Правил пока нет."),
        b.as_markup(),
    )


@router.callback_query(F.data == "menu:autoreply")
async def autoreply_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show_autoreply(call)


@router.callback_query(F.data == "autoreply:add")
async def autoreply_add(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.set_state(AutoReplyAdd.trigger)
    await state.update_data(account_id=str(account.id))
    await call.answer()
    await edit(
        call,
        "Введите ключевую фразу или <code>*</code> для любого входящего сообщения.",
        back_menu("autoreply"),
    )


@router.message(AutoReplyAdd.trigger)
async def autoreply_trigger(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value:
        return
    await state.update_data(trigger=value[:255])
    await state.set_state(AutoReplyAdd.response)
    await message.answer("Теперь отправьте текст ответа.")


@router.message(AutoReplyAdd.response)
async def autoreply_response(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    data = await state.get_data()
    if not value:
        return
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, uuid.UUID(data["account_id"]))
        if not account or account.tg_user_id != message.from_user.id:
            await state.clear()
            return
        session.add(AutoReplyRule(
            account_id=account.id,
            trigger=data["trigger"],
            response=value,
            enabled=True,
        ))
        await session.commit()
    await state.clear()
    await message.answer("✅ Правило добавлено.", reply_markup=main_menu())


@router.callback_query(F.data.startswith("autoreply:delete:"))
async def autoreply_delete(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(AutoReplyRule, rule_id)
        if rule and rule.account_id == account.id:
            await session.delete(rule)
            await session.commit()
    await call.answer("Удалено")
    await show_autoreply(call)


async def show_delivery(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        rules = list((await session.scalars(
            select(DeliveryRule)
            .where(DeliveryRule.account_id == account.id)
            .order_by(DeliveryRule.id)
        )).all())
        counts: dict[int, int] = {}
        for rule in rules:
            if rule.mode == "stock":
                counts[rule.id] = int(await session.scalar(
                    select(func.count(DeliveryStock.id)).where(
                        DeliveryStock.rule_id == rule.id,
                        DeliveryStock.used_at.is_(None),
                    )
                ) or 0)
    lines = ["⚡ <b>Автовыдача</b>"]
    b = InlineKeyboardBuilder()
    for rule in rules:
        suffix = f" · остаток {counts.get(rule.id, 0)}" if rule.mode == "stock" else ""
        lines.append(f"• <code>{html.escape(rule.item_id)}</code> — {rule.mode}{suffix}")
        b.button(text=f"🗑 {clip(rule.item_id, 28)}", callback_data=f"delivery:delete:{rule.id}")
    if not rules:
        lines.append("Правил пока нет.")
    lines.append("\nID <code>*</code> — правило по умолчанию для всех товаров.")
    b.button(text="➕ Добавить правило", callback_data="delivery:add")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    await edit(call, "\n".join(lines), b.as_markup())


@router.callback_query(F.data == "menu:delivery")
async def delivery_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show_delivery(call)


@router.callback_query(F.data == "delivery:add")
async def delivery_add(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.set_state(DeliveryAdd.item_id)
    await state.update_data(account_id=str(account.id))
    await call.answer()
    await edit(call, "Введите Playerok Item ID или <code>*</code>.", back_menu("delivery"))


@router.message(DeliveryAdd.item_id)
async def delivery_item(message: Message, state: FSMContext) -> None:
    item_id = (message.text or "").strip()
    if not item_id:
        return
    await state.update_data(item_id=item_id[:128])
    await state.set_state(DeliveryAdd.mode)
    await message.answer("Выберите режим.", reply_markup=delivery_mode_menu())


@router.callback_query(DeliveryAdd.mode, F.data.startswith("delivery_mode:"))
async def delivery_mode(call: CallbackQuery, state: FSMContext) -> None:
    mode = call.data.split(":", 1)[1]
    if mode not in {"static", "stock"}:
        return
    await state.update_data(mode=mode)
    await state.set_state(DeliveryAdd.content)
    await call.answer()
    if mode == "static":
        text = (
            "Отправьте текст выдачи. Можно использовать "
            "<code>{buyer}</code>, <code>{item}</code>, <code>{deal_id}</code>."
        )
    else:
        text = "Отправьте склад: одна непустая строка = одна одноразовая выдача."
    await edit(call, text, back_menu("delivery"))


@router.message(DeliveryAdd.content)
async def delivery_content(message: Message, state: FSMContext) -> None:
    content = (message.text or "").strip()
    if not content:
        return
    data = await state.get_data()
    account_id = uuid.UUID(data["account_id"])
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, account_id)
        if not account or account.tg_user_id != message.from_user.id:
            await state.clear()
            return
        old = await session.scalar(select(DeliveryRule).where(
            DeliveryRule.account_id == account_id,
            DeliveryRule.item_id == data["item_id"],
        ))
        if old:
            await session.delete(old)
            await session.flush()
        rule = DeliveryRule(
            account_id=account_id,
            item_id=data["item_id"],
            mode=data["mode"],
            message_template=content if data["mode"] == "static" else "{stock}",
            enabled=True,
        )
        session.add(rule)
        await session.flush()
        if data["mode"] == "stock":
            for line in (x.strip() for x in content.splitlines()):
                if line:
                    session.add(DeliveryStock(
                        rule_id=rule.id,
                        payload_encrypted=svc().cipher.encrypt(line),
                    ))
        await session.commit()
    await state.clear()
    await message.answer("✅ Автовыдача сохранена.", reply_markup=main_menu())


@router.callback_query(F.data.startswith("delivery:delete:"))
async def delivery_delete(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    rule_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        rule = await session.get(DeliveryRule, rule_id)
        if rule and rule.account_id == account.id:
            await session.delete(rule)
            await session.commit()
    await call.answer("Удалено")
    await show_delivery(call)


async def show_items(call: CallbackQuery) -> None:
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои товары", callback_data="items:list")],
        [InlineKeyboardButton(text="🧩 Шаблоны товаров", callback_data="templates:list")],
        [InlineKeyboardButton(text="➕ Выставить товар", callback_data="items:create")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main")],
    ])
    await edit(call, "📦 <b>Товары Playerok</b>", markup)


@router.callback_query(F.data == "templates:list")
async def templates_list(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        rows = list((await session.scalars(
            select(ItemTemplate).where(ItemTemplate.account_id == account.id).order_by(ItemTemplate.created_at.desc())
        )).all())
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.button(text=f"📄 {clip(row.name, 32)}", callback_data=f"template:use:{row.id}")
        builder.button(text="🗑", callback_data=f"template:delete:{row.id}")
    builder.button(text="⬅️ Товары", callback_data="menu:items")
    builder.adjust(2, 1)
    text = "🧩 <b>Шаблоны товаров</b>\n\n" + ("\n".join(f"• {html.escape(row.name)}" for row in rows) or "Шаблонов пока нет.")
    await call.answer()
    await edit(call, text, builder.as_markup())


@router.callback_query(F.data.startswith("template:delete:"))
async def template_delete(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    template_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        row = await session.get(ItemTemplate, template_id)
        if row and row.account_id == account.id:
            await session.delete(row)
            await session.commit()
    await call.answer("Шаблон удалён")
    await templates_list(call)


@router.callback_query(F.data.startswith("template:use:"))
async def template_use(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    template_id = int(call.data.rsplit(":", 1)[-1])
    async with svc().db() as session:
        row = await session.get(ItemTemplate, template_id)
    if not row or row.account_id != account.id:
        await call.answer("Шаблон не найден", show_alert=True)
        return
    await state.clear()
    await state.update_data(account_id=str(account.id), **row.payload)
    await call.answer()
    await create_draft(call.message, state, call.from_user.id)


@router.callback_query(F.data == "menu:items")
async def items_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await require_account(call):
        return
    await call.answer()
    await show_items(call)


@router.callback_query(F.data == "items:create")
async def item_create(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    await state.clear()
    await state.set_state(ItemCreate.game)
    await state.update_data(account_id=str(account.id))
    await call.answer()
    await edit(call, "🎮 Отправьте slug игры/приложения Playerok.", back_menu("items"))


@router.message(ItemCreate.game)
async def item_game(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, uuid.UUID(data["account_id"]))
    try:
        client = await svc().gateway.get_client(account)
        game = await client.call("get_game", slug=(message.text or "").strip().lower())
    except Exception as exc:
        await message.answer(f"❌ Игра не найдена: <code>{html.escape(str(exc))[:900]}</code>", parse_mode="HTML")
        return
    categories = [(str(c.id), str(c.name)) for c in list(getattr(game, "categories", []) or [])[:30]]
    if not categories:
        await message.answer("Категорий нет.")
        return
    await state.update_data(categories=categories)
    await state.set_state(ItemCreate.category)
    b = InlineKeyboardBuilder()
    for i, (_id, name) in enumerate(categories):
        b.button(text=clip(name, 36), callback_data=f"itemcat:{i}")
    b.adjust(1)
    await message.answer("Выберите категорию:", reply_markup=b.as_markup())


@router.callback_query(ItemCreate.category, F.data.startswith("itemcat:"))
async def item_category(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(call.data.split(":", 1)[1])
    category_id, category_name = data["categories"][idx]
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, uuid.UUID(data["account_id"]))
    try:
        client = await svc().gateway.get_client(account)
        page = await client.call("get_game_category_obtaining_types", category_id, count=24)
        values = [(str(x.id), str(x.name)) for x in list(getattr(page, "obtaining_types", []) or [])]
    except Exception as exc:
        await call.answer("Ошибка API", show_alert=True)
        await call.message.answer(f"<code>{html.escape(str(exc))[:1200]}</code>", parse_mode="HTML")
        return
    if not values:
        await call.answer("Нет способов получения", show_alert=True)
        return
    await state.update_data(category_id=category_id, category_name=category_name, obtaining=values)
    await state.set_state(ItemCreate.obtaining)
    b = InlineKeyboardBuilder()
    for i, (_id, name) in enumerate(values):
        b.button(text=clip(name, 38), callback_data=f"itemobt:{i}")
    b.adjust(1)
    await call.answer()
    await edit(call, f"Способ получения для <b>{html.escape(category_name)}</b>:", b.as_markup())


@router.callback_query(ItemCreate.obtaining, F.data.startswith("itemobt:"))
async def item_obtaining(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(call.data.split(":", 1)[1])
    obtaining_id, obtaining_name = data["obtaining"][idx]
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, uuid.UUID(data["account_id"]))
    try:
        client = await svc().gateway.get_client(account)
        category = await client.call("get_game_category", id=data["category_id"])
        
        # Запрашиваем ВСЕ поля категории (включая логин, пароль, почту, формат выдачи)
        fields = []
        try:
            fields_page = await client.call(
                "get_game_category_data_fields",
                data["category_id"],
                obtaining_id,
                count=50,
                type=None,
            )
            fields = list(getattr(fields_page, "data_fields", []) or [])
        except Exception:
            pass

        if not fields:
            # Fallback: объединяем ITEM_DATA и OBTAINING_DATA
            try:
                p1 = await client.call("get_game_category_data_fields", data["category_id"], obtaining_id, count=50, type=GameCategoryDataFieldTypes.ITEM_DATA)
                p2 = await client.call("get_game_category_data_fields", data["category_id"], obtaining_id, count=50, type=GameCategoryDataFieldTypes.OBTAINING_DATA)
                seen_fids = set()
                for f in (getattr(p1, "data_fields", []) or []) + (getattr(p2, "data_fields", []) or []):
                    fid = str(getattr(f, "id", ""))
                    if fid and fid not in seen_fids:
                        seen_fids.add(fid)
                        fields.append(f)
            except Exception:
                pass
    except Exception as exc:
        await call.answer("Ошибка API", show_alert=True)
        await call.message.answer(f"<code>{html.escape(str(exc))[:1200]}</code>", parse_mode="HTML")
        return

    options = list(getattr(category, "options", []) or [])
    field_meta = [
        (
            str(f.id),
            str(f.label),
            bool(f.required),
            bool(getattr(f, "hidden", False)),
            str(getattr(getattr(f, "type", None), "name", "")),
        )
        for f in fields
    ]

    # Группируем опции по их полю и названию группы
    groups_dict: dict[str, dict[str, Any]] = {}
    for o in options:
        fld = str(o.field)
        grp = str(o.group or fld)
        if fld not in groups_dict:
            groups_dict[fld] = {
                "field": fld,
                "group_name": grp,
                "multiple": bool(getattr(o, "multiple", False)),
                "choices": [],
            }
        groups_dict[fld]["choices"].append((str(o.value), str(o.label)))

    option_groups = list(groups_dict.values())

    await state.update_data(
        obtaining_id=obtaining_id,
        obtaining_name=obtaining_name,
        field_meta=field_meta,
        option_groups=option_groups,
        attribute_group_index=0,
        attributes={},
    )

    if option_groups:
        await _show_attribute_group(call, state)
    else:
        await state.set_state(ItemCreate.data_fields)
        await call.message.answer("Атрибутов нет. Переходим к полям товара.")
        await _start_item_fields(call.message, state)


async def _show_attribute_group(call_or_msg: Any, state: FSMContext) -> None:
    data = await state.get_data()
    groups = data.get("option_groups") or []
    idx = int(data.get("attribute_group_index", 0))
    if idx >= len(groups):
        target = call_or_msg.message if isinstance(call_or_msg, CallbackQuery) else call_or_msg
        await _start_item_fields(target, state)
        return

    grp = groups[idx]
    group_name = grp["group_name"]
    choices = grp["choices"]

    b = InlineKeyboardBuilder()
    for i, (_val, label) in enumerate(choices[:30]):
        b.button(text=clip(label, 36), callback_data=f"itemopt:{idx}:{i}")
    b.button(text="⏭ Пропустить этот атрибут", callback_data=f"itemopt:{idx}:skip")
    b.adjust(1)

    prompt = (
        f"⚙️ <b>Параметр {idx + 1}/{len(groups)}: {html.escape(group_name)}</b>\n"
        "Выберите подходящее значение:"
    )
    await state.set_state(ItemCreate.attributes)
    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.answer()
        await edit(call_or_msg, prompt, b.as_markup())
    else:
        await call_or_msg.answer(prompt, reply_markup=b.as_markup(), parse_mode="HTML")


async def _format_field_prompt(field_tuple: tuple[str, str, bool, bool, str], current_index: int, total_count: int) -> str:
    _fid, label, required, is_hidden, type_name = field_tuple
    lbl_lower = label.lower()
    is_obtaining = type_name == "OBTAINING_DATA" or any(k in lbl_lower for k in ("логин", "login", "почт", "email", "пароль", "password", "ключ", "выдач", "аккаунт", "токен", "код"))
    
    if is_obtaining or is_hidden:
        header = f"🔐 <b>Данные выдачи {current_index}/{total_count} (для покупателя):</b>\n"
        hint = "\n<i>(Эти данные передаются покупателю автоматически после оплаты)</i>"
    else:
        header = f"🧾 <b>Поле товара {current_index}/{total_count}:</b>\n"
        hint = ""

    req_str = " <b>(обязательно)</b>" if required else " <i>(можно пропустить, отправив «-»)</i>"
    return f"{header}Поле: «<b>{html.escape(label)}</b>»{req_str}{hint}\n\nВведите значение:"


async def _start_item_fields(target: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data.get("field_meta") or []
    if not fields:
        await state.update_data(data_field_values={})
        await state.set_state(ItemCreate.name)
        await target.answer("Название товара:")
        return
    await state.update_data(field_index=0, data_field_values={})
    await state.set_state(ItemCreate.data_fields)
    prompt = await _format_field_prompt(fields[0], 1, len(fields))
    await target.answer(prompt, parse_mode="HTML")


@router.callback_query(ItemCreate.attributes, F.data.startswith("itemopt:"))
async def item_option(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    parts = call.data.split(":")
    # Формат: itemopt:<group_index>:<choice_index или skip>
    if len(parts) == 3:
        grp_idx = int(parts[1])
        choice_raw = parts[2]
    else:
        # Обратная совместимость
        grp_idx = int(data.get("attribute_group_index", 0))
        choice_raw = parts[1]

    groups = data.get("option_groups") or []
    attributes = dict(data.get("attributes") or {})

    if 0 <= grp_idx < len(groups) and choice_raw != "skip":
        grp = groups[grp_idx]
        choice_idx = int(choice_raw)
        if 0 <= choice_idx < len(grp["choices"]):
            val, _lbl = grp["choices"][choice_idx]
            attributes[grp["field"]] = val

    next_idx = grp_idx + 1
    await state.update_data(attributes=attributes, attribute_group_index=next_idx)

    if next_idx < len(groups):
        await _show_attribute_group(call, state)
    else:
        await call.answer()
        await _start_item_fields(call.message, state)


@router.message(ItemCreate.data_fields)
async def item_fields(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data.get("field_meta") or []
    index = int(data.get("field_index", 0))
    if index >= len(fields):
        await _start_item_fields(message, state)
        return
    field_id, label, required, _hidden, _type_name = fields[index]
    raw_value = (message.text or "").strip()
    # Если поле обязательно, не разрешаем пустой ввод или пропуск через "-"
    if required and (not raw_value or raw_value in {"-", "—", "пропуск", "skip"}):
        await message.answer(f"⚠️ Поле «{label}» <b>обязательно</b> для выставления этого лота. Пожалуйста, введите корректное значение:", parse_mode="HTML")
        return
    values = dict(data.get("data_field_values") or {})
    if raw_value and raw_value not in {"-", "—", "пропуск", "skip"}:
        values[field_id] = raw_value
    index += 1
    if index < len(fields):
        await state.update_data(data_field_values=values, field_index=index)
        prompt = await _format_field_prompt(fields[index], index + 1, len(fields))
        await message.answer(prompt, parse_mode="HTML")
        return
    await state.update_data(data_field_values=values)
    await state.set_state(ItemCreate.name)
    await message.answer("🏷 <b>Название товара:</b>", parse_mode="HTML")


@router.message(ItemCreate.name)
async def item_name(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value:
        return
    await state.update_data(item_name=value)
    await state.set_state(ItemCreate.price)
    await message.answer("💰 <b>Цена в ₽ (целое число от 1 до 10 000 000):</b>", parse_mode="HTML")


@router.message(ItemCreate.price)
async def item_price(message: Message, state: FSMContext) -> None:
    try:
        clean_val = (message.text or "").strip().replace(" ", "").replace("₽", "").replace("руб", "").replace("р", "")
        value = int(clean_val)
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите положительное целое число от 1 до 10 000 000 ₽:")
        return
    await state.update_data(item_price=value)
    await state.set_state(ItemCreate.description)
    await message.answer("📝 <b>Описание товара:</b>", parse_mode="HTML")


@router.message(ItemCreate.description)
async def item_description(message: Message, state: FSMContext) -> None:
    await state.update_data(item_description=(message.text or "").strip(), attachments_b64=[])
    await state.set_state(ItemCreate.image)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без фотографий (создать)", callback_data="itemimage:skip")]
    ])
    await message.answer(
        "📸 <b>Фотографии товара</b>\n\n"
        "Отправьте фотографии (обложку, скриншоты лота) прямо в этот чат.\n"
        "Вы можете отправить до 10 фотографий по очереди или нажать кнопку ниже, чтобы выставить без фото:",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(ItemCreate.image, F.data == "itemimage:skip")
async def item_image_skip(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await create_draft(call.message, state, call.from_user.id)


@router.callback_query(ItemCreate.image, F.data == "itemimage:done")
async def item_image_done(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await create_draft(call.message, state, call.from_user.id)


@router.message(ItemCreate.image, F.photo)
async def item_image(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    current_list = list(data.get("attachments_b64") or [])
    if len(current_list) >= 10:
        await message.answer("⚠️ Достигнут лимит 10 фотографий на лот. Нажмите кнопку создания лота ниже:")
        return

    buffer = io.BytesIO()
    await bot.download(message.photo[-1], destination=buffer)
    b64_str = base64.b64encode(buffer.getvalue()).decode()
    current_list.append(b64_str)
    await state.update_data(attachments_b64=current_list)

    count = len(current_list)
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Завершить и создать лот ({count} фото)", callback_data="itemimage:done")
    b.adjust(1)

    await message.answer(
        f"📸 <b>Фото #{count} добавлено!</b>\n\n"
        "Вы можете отправить следующее фото или завершить создание лота:",
        reply_markup=b.as_markup(),
        parse_mode="HTML",
    )


async def create_draft(target: Message, state: FSMContext, tg_id: int) -> None:
    data = await state.get_data()
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, uuid.UUID(data["account_id"]))
    if not account or account.tg_user_id != tg_id:
        await state.clear()
        return

    wait_msg = await target.answer("⏳ Создаю товар на Playerok…")
    try:
        client = await svc().gateway.get_client(account)

        # Собираем ВСЕ поля (параметры товара + логин/пароль/формат выдачи)
        values = data.get("data_field_values") or {}
        payload_fields = [{"fieldId": str(fid), "value": str(fval)} for fid, fval in values.items()]

        # Декодируем все загруженные фотографии
        attachments = []
        for b64 in (data.get("attachments_b64") or []):
            if b64:
                attachments.append(base64.b64decode(b64))

        item = await client.create_item(
            game_category_id=data["category_id"],
            obtaining_type_id=data["obtaining_id"],
            name=data["item_name"],
            price=data["item_price"],
            description=data["item_description"],
            options=data.get("attributes") or {},
            data_fields=payload_fields,
            attachments=attachments,
        )
        priorities = await client.get_item_priority_statuses(str(item.id), data["item_price"])
    except Exception as exc:
        await wait_msg.delete()
        await target.answer(
            f"❌ Не удалось создать черновик:\n<code>{html.escape(str(exc))[:1800]}</code>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        await state.clear()
        return

    await wait_msg.delete()
    packed = [(str(p.id), str(p.name), int(p.price or 0)) for p in priorities]
    await state.update_data(new_item_id=str(item.id), priorities=packed)
    await state.set_state(ItemCreate.priority)
    b = InlineKeyboardBuilder()
    for i, (_id, name, price) in enumerate(packed):
        b.button(text=f"{clip(name, 26)} · {price} ₽", callback_data=f"itemprio:{i}")
    b.button(text="💾 Сохранить как шаблон", callback_data="itemtemplate:save")
    b.button(text="Оставить черновиком", callback_data="itemprio:draft")
    b.adjust(1)
    await target.answer(
        f"✅ Черновик создан: <code>{html.escape(str(item.id))}</code>\nВыберите публикацию.",
        reply_markup=b.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(ItemCreate.priority, F.data == "itemtemplate:save")
async def item_template_save_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ItemCreate.template_name)
    await call.answer()
    await call.message.answer("Введите название шаблона:")


@router.message(ItemCreate.template_name)
async def item_template_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не должно быть пустым.")
        return
    data = await state.get_data()
    payload = {
        key: data[key]
        for key in (
            "category_id", "category_name", "obtaining_id", "obtaining_name", "attributes",
            "data_field_values", "item_name", "item_price", "item_description",
        )
        if key in data
    }
    async with svc().db() as session:
        session.add(ItemTemplate(account_id=uuid.UUID(data["account_id"]), name=name, payload=payload))
        await session.commit()
    await state.clear()
    await message.answer("✅ Шаблон сохранён.", reply_markup=main_menu())


@router.callback_query(ItemCreate.priority, F.data == "itemprio:draft")
async def leave_draft(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Оставлено в черновиках")
    await edit(call, "Товар сохранён как черновик.", main_menu())


@router.callback_query(ItemCreate.priority, F.data.startswith("itemprio:"))
async def item_publish(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(call.data.split(":", 1)[1])
    priority_id, _name, _price = data["priorities"][idx]
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, uuid.UUID(data["account_id"]))
    try:
        client = await svc().gateway.get_client(account)
        item = await client.call("publish_item", data["new_item_id"], priority_id)
    except Exception as exc:
        await call.answer("Ошибка публикации", show_alert=True)
        await call.message.answer(f"<code>{html.escape(str(exc))[:1600]}</code>", parse_mode="HTML")
        return
    await state.clear()
    await call.answer("Опубликовано")
    await edit(
        call,
        f"✅ Опубликовано: <b>{html.escape(str(getattr(item, 'name', 'Товар')))}</b>\n"
        f"ID: <code>{html.escape(str(getattr(item, 'id', data['new_item_id'])))}</code>",
        main_menu(),
    )


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Используйте меню ниже.", reply_markup=main_menu())
