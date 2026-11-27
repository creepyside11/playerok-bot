from __future__ import annotations

import base64
import copy
import html
import io
import logging
import time
import uuid
from typing import Any
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import mini_app_url
from .crypto import SecretCipher
from .item_delivery import (
    BUYER_MESSAGE_ERROR, BUYER_MESSAGE_PROMPT, SKIP_VALUES, buyer_message_field_id,
    get_item_data_fields, item_delivery_payload, valid_buyer_message,
)
from .item_publication import parse_discount_price, tariff_price
from .keyboards import auth_method_menu, back_menu, delivery_mode_menu, main_menu
from .models import AutoReplyRule, DeliveryRule, DeliveryStock, ItemTemplate, PlayerokAccount, TelegramUser, fresh_settings
from .playerok_errors import playerok_error_text
from .playerok import DEFAULT_USER_AGENT, EmailAuthClient, PlayerokGateway, normalize_proxy
from .states import AddAccount, AutoReplyAdd, DeliveryAdd, ItemCreate, WithdrawalPreview
from .withdrawal_history import PLAYEROK_CONTACTS_URL, PLAYEROK_URL, payout_history
from .withdrawal_preview import (
    PreviewUnavailable, current_withdrawable, format_money, parse_details_suffix,
    parse_preview_amount, remaining_balance,
)


logger = logging.getLogger(__name__)
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


async def edit(call: CallbackQuery, text: str, markup=None, **kwargs) -> None:
    message = call.message
    if message is None:
        # Инлайн-сообщения (или удалённое исходное) не имеют message — шлём новое.
        await call.bot.send_message(
            call.from_user.id, text, reply_markup=markup, parse_mode="HTML", **kwargs
        )
        return
    try:
        await message.edit_text(text, reply_markup=markup, parse_mode="HTML", **kwargs)
    except TelegramBadRequest as exc:
        # Повторное нажатие «Обновить» с тем же текстом не должно плодить
        # дубликаты сообщений.
        if "message is not modified" in str(exc).lower():
            return
        await message.answer(text, reply_markup=markup, parse_mode="HTML", **kwargs)
    except Exception:
        await message.answer(text, reply_markup=markup, parse_mode="HTML", **kwargs)


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
    result["auto_confirm_mode"] = str(raw.get("auto_confirm_mode", "all"))
    result["auto_confirm_items"] = list(raw.get("auto_confirm_items") or [])
    result["auto_confirm_categories"] = list(raw.get("auto_confirm_categories") or [])
    result["notifications"].update(raw.get("notifications") or {})
    return result


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandObject | None = None) -> None:
    await state.clear()
    account = await active_account(message.from_user.id)
    if command and command.args == "tg_accounts" and account and message.chat.type == ChatType.PRIVATE:
        await message.answer(
            f"ТГ-аккаунты · @{account.username}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Открыть склад в боте", callback_data="tgacc:manager")],
                [InlineKeyboardButton(text="Управление плагином", callback_data="tgacc:open")],
            ]),
        )
        return
    tail = f"\n\nАктивный: <b>{html.escape(account.username)}</b>" if account else "\n\nPlayerok аккаунт не подключён."
    await message.answer(
        "🤖 <b>Playerok Bot</b>\nУправление магазином и автоматизация." + tail,
        reply_markup=main_menu(private=message.chat.type == ChatType.PRIVATE),
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
    private = bool(call.message and call.message.chat.type == ChatType.PRIVATE)
    await edit(call, "🤖 <b>Playerok Bot</b>" + tail, main_menu(private=private))


@router.callback_query(F.data == "menu:root")
async def menu_root(call: CallbackQuery, state: FSMContext) -> None:
    # Совместимость со старыми сообщениями, где использовался callback menu:root.
    await menu_main(call, state)


@router.callback_query(F.data == "noop")
async def noop_callback(call: CallbackQuery) -> None:
    # Кнопки-счётчики страниц не должны оставлять «часики» на клиенте.
    await call.answer()


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
    b.button(text="🟢 Открыть Mini App", callback_data="menu:web")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    await edit(call, "🔐 <b>Playerok аккаунты</b>", b.as_markup())


@router.callback_query(F.data.in_({"menu:web", "web:reset_password"}))
async def miniapp_menu(call: CallbackQuery) -> None:
    url = mini_app_url()
    await call.answer()
    markup = InlineKeyboardBuilder()
    if url and call.message and call.message.chat.type == ChatType.PRIVATE:
        text = "<b>Панель продавца</b>\n\nЧаты, сделки и плагины Playerok."
        markup.button(text="Открыть Mini App", web_app=WebAppInfo(url=url))
    else:
        text = "Откройте Mini App из личного чата с ботом." if url else "Mini App пока не подключено."
    markup.button(text="Назад", callback_data="menu:main")
    markup.adjust(1)
    await edit(call, text, markup.as_markup())


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
async def balance(call: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() in WithdrawalPreview.__all_states_names__:
        await state.clear()
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
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Вывод", callback_data="balance:withdrawals")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu:balance")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
    ])
    await edit(call, text, markup)


@router.callback_query(F.data == "balance:withdrawals")
async def withdrawals(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте раздел в личном чате с ботом.", show_alert=True)
        return
    account = await require_account(call)
    if not account:
        return
    if account.tg_user_id != call.from_user.id:
        await call.answer("Аккаунт не найден.", show_alert=True)
        return
    if await state.get_state() in WithdrawalPreview.__all_states_names__:
        await state.clear()
    await call.answer("Проверяю историю…")
    parts = [f"💸 <b>Вывод средств · @{html.escape(account.username)}</b>",
             "Оформление выплаты и ввод полных реквизитов — только на сайте Playerok.",
             "<b>Последние выплаты</b>"]
    try:
        client = await svc().gateway.get_client(account)
        result = await payout_history(client, account.playerok_user_id)
        for entry in result["items"]:
            money = html.escape(entry["amount"] or "—")
            label = html.escape(entry["statusLabel"])
            details = html.escape(entry["details"] or "—")
            created = html.escape((entry["time"] or "—")[:30])
            parts.append(f"<b>{money} ₽</b> · {label}\nРеквизиты: {details}\nДата: {created}")
        if not result["items"]:
            parts.append("На этой странице выплат нет.")
    except Exception:
        parts.append("Не удалось получить историю. Проверьте статус на сайте Playerok.")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Рассчитать вывод", callback_data="payout_preview:start")],
        [InlineKeyboardButton(text="🌐 Оформить на Playerok", url=PLAYEROK_URL)],
        [InlineKeyboardButton(text="🔄 Обновить статусы", callback_data="balance:withdrawals")],
        [InlineKeyboardButton(text="Помощь с выплатами", url=PLAYEROK_CONTACTS_URL)],
        [InlineKeyboardButton(text="⬅️ Баланс", callback_data="menu:balance")],
    ])
    await edit(call, "\n\n".join(parts), markup)

PREVIEW_LIFETIME = 10 * 60


def preview_cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отменить", callback_data="balance:withdrawals")],
    ])


def preview_result_markup(nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтверждаю: средства мои", callback_data=f"payout_preview:confirm:{nonce}")],
        [InlineKeyboardButton(text="Изменить сумму", callback_data="payout_preview:start")],
        [InlineKeyboardButton(text="Отменить", callback_data="balance:withdrawals")],
    ])


def preview_official_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти на Playerok", url=PLAYEROK_URL)],
        [InlineKeyboardButton(text="Помощь с выплатами", url=PLAYEROK_CONTACTS_URL)],
        [InlineKeyboardButton(text="⬅️ История вывода", callback_data="balance:withdrawals")],
    ])


async def preview_account(tg_id: int, state: FSMContext) -> PlayerokAccount:
    data = await state.get_data()
    account = await active_account(tg_id)
    created = data.get("preview_created")
    if (
        not account or account.tg_user_id != tg_id
        or str(account.id) != data.get("preview_account")
        or not isinstance(created, (int, float))
        or not 0 <= time.monotonic() - created <= PREVIEW_LIFETIME
    ):
        await state.clear()
        raise PreviewUnavailable("Расчёт устарел или аккаунт изменился. Начните заново.")
    return account


async def preview_available(account: PlayerokAccount) -> Decimal:
    client = await svc().gateway.get_client(account)
    return await current_withdrawable(client, account.playerok_user_id)


async def invalidate_preview(state: FSMContext, exc: Exception) -> str:
    await state.clear()
    if isinstance(exc, (PreviewUnavailable, ValueError)):
        # Internal validation errors carry a user-safe message.
        if isinstance(exc, PreviewUnavailable):
            return str(exc)
        return "Сумма больше доступного остатка или расчёт устарел. Начните заново."
    return "Не удалось проверить текущий баланс. Начните расчёт заново или откройте Playerok."


def preview_text(account: PlayerokAccount, available: Decimal, requested: Decimal, suffix: str) -> str:
    remainder = remaining_balance(available, requested)
    return (
        "📝 <b>Предварительный расчёт вывода</b>\n"
        f"Аккаунт: @{html.escape(account.username)}\n\n"
        f"Доступно к выводу: <b>{format_money(available)}</b>\n"
        f"Сумма: <b>{format_money(requested)}</b>\n"
        f"Расчётный остаток: <b>{format_money(remainder)}</b>\n"
        f"Реквизиты: <b>•••• {suffix}</b> (проверен только формат)\n\n"
        "Комиссия и фактический остаток определяются на сайте. "
        "Подтвердите, что средства принадлежат вам. Это подтверждение расчёта, не перевод."
    )


@router.callback_query(F.data == "payout_preview:start")
async def preview_start(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    account = await active_account(call.from_user.id)
    if not account or account.tg_user_id != call.from_user.id:
        await call.answer("Сначала подключите свой аккаунт.", show_alert=True)
        return
    await state.clear()
    await call.answer("Проверяю аккаунт и баланс…")
    try:
        available = await preview_available(account)
    except Exception as exc:
        await edit(call, await invalidate_preview(state, exc), preview_official_markup())
        return
    if available <= 0:
        await edit(call, "Сейчас нет средств, доступных к выводу.", preview_official_markup())
        return
    await state.set_data({
        "preview_account": str(account.id), "preview_created": time.monotonic(),
    })
    await state.set_state(WithdrawalPreview.amount)
    await edit(call, (
        f"📝 <b>Расчёт вывода · @{html.escape(account.username)}</b>\n\n"
        f"Доступно к выводу: <b>{format_money(available)}</b>\n"
        "Введите сумму в рублях (например, 500 или 500,50).\n\n"
        "Это предварительный расчёт без списания средств."
    ), preview_cancel_markup())


@router.message(WithdrawalPreview.amount, F.chat.type == ChatType.PRIVATE)
async def preview_amount(message: Message, state: FSMContext) -> None:
    try:
        requested = parse_preview_amount(message.text)
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=preview_cancel_markup())
        return
    try:
        account = await preview_account(message.from_user.id, state)
        available = await preview_available(account)
    except Exception as exc:
        await message.answer(await invalidate_preview(state, exc), reply_markup=preview_official_markup())
        return
    if requested > available:
        await message.answer(
            f"Недостаточно средств. Доступно к выводу: {format_money(available)}. Введите меньшую сумму.",
            reply_markup=preview_cancel_markup(),
        )
        return
    await state.update_data(preview_amount=str(requested))
    await state.set_state(WithdrawalPreview.last_four)
    await message.answer(
        "Введите только последние 4 цифры карты или номера телефона, чтобы сверить реквизиты. "
        "Не отправляйте полный номер, CVV, PIN или коды подтверждения.",
        reply_markup=preview_cancel_markup(),
    )


@router.message(WithdrawalPreview.last_four, F.chat.type == ChatType.PRIVATE)
async def preview_details(message: Message, state: FSMContext) -> None:
    # Best effort: remove the user's input even when they accidentally sent full details.
    try:
        await message.delete()
    except Exception:
        pass
    try:
        suffix = parse_details_suffix(message.text)
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=preview_cancel_markup())
        return
    try:
        account = await preview_account(message.from_user.id, state)
        available = await preview_available(account)
        requested = parse_preview_amount((await state.get_data()).get("preview_amount"))
        remaining_balance(available, requested)
    except Exception as exc:
        await message.answer(await invalidate_preview(state, exc), reply_markup=preview_official_markup())
        return
    nonce = uuid.uuid4().hex[:16]
    await state.update_data(
        preview_suffix=suffix, preview_available=str(available), preview_nonce=nonce,
    )
    await state.set_state(WithdrawalPreview.confirm)
    await message.answer(
        preview_text(account, available, requested, suffix),
        reply_markup=preview_result_markup(nonce), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("payout_preview:confirm:"))
async def preview_confirm(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    data = await state.get_data()
    if (
        await state.get_state() != WithdrawalPreview.confirm.state
        or call.data.rsplit(":", 1)[-1] != data.get("preview_nonce")
    ):
        await call.answer("Этот расчёт уже завершён или устарел.", show_alert=True)
        return
    await call.answer("Повторно проверяю баланс…")
    try:
        account = await preview_account(call.from_user.id, state)
        available = await preview_available(account)
        requested = parse_preview_amount(data.get("preview_amount"))
        suffix = parse_details_suffix(data.get("preview_suffix"))
        remaining_balance(available, requested)
        quoted_balance = parse_preview_amount(data.get("preview_available"))
    except Exception as exc:
        await edit(call, await invalidate_preview(state, exc), preview_official_markup())
        return
    if available != quoted_balance:
        nonce = uuid.uuid4().hex[:16]
        await state.update_data(preview_available=str(available), preview_nonce=nonce)
        await edit(call, "Баланс изменился. Проверьте расчёт повторно.\n\n" + preview_text(
            account, available, requested, suffix,
        ), preview_result_markup(nonce))
        return
    await state.clear()
    await edit(call, (
        "✅ <b>Расчёт подтверждён</b>\n\n"
        f"Сумма: <b>{format_money(requested)}</b>\n"
        f"Расчётный остаток: <b>{format_money(remaining_balance(available, requested))}</b>\n"
        f"Реквизиты для сверки: <b>•••• {suffix}</b>\n\n"
        "Средства не списаны, заявка не создана. Оформите выплату на Playerok; там же "
        "проверяются полные реквизиты и комиссия. После создания заявки её статус будет доступен в истории."
    ), preview_official_markup())



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
    cfg = settings(account)
    enabled = cfg["auto_confirm"]
    mode = cfg.get("auto_confirm_mode", "all")
    selected_items = cfg.get("auto_confirm_items") or []
    selected_categories = cfg.get("auto_confirm_categories") or []

    mode_names = {
        "all": "🌐 Все лоты",
        "items": f"🎯 Выбранные лоты ({len(selected_items)})",
        "categories": f"🗂 По категориям ({len(selected_categories)})",
    }
    mode_label = mode_names.get(mode, "🌐 Все лоты")

    rows = [
        [InlineKeyboardButton(
            text="✅ Включено" if enabled else "❌ Выключено",
            callback_data="autoconfirm:toggle",
        )],
        [
            InlineKeyboardButton(text="🌐 Все", callback_data="autoconfirm:set_mode:all"),
            InlineKeyboardButton(text="🎯 По лотам", callback_data="autoconfirm:set_mode:items"),
            InlineKeyboardButton(text="🗂 Категории", callback_data="autoconfirm:set_mode:categories"),
        ],
    ]

    if mode == "items":
        rows.append([InlineKeyboardButton(text=f"📦 Выбрать лоты ({len(selected_items)} выбрано)", callback_data="autoconfirm:items:0")])
    elif mode == "categories":
        rows.append([InlineKeyboardButton(text=f"🗂 Выбрать категории ({len(selected_categories)} выбрано)", callback_data="autoconfirm:categories")])

    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    desc_mode = {
        "all": "Автоматически подтверждаются <b>все</b> оплаченные лоты.",
        "items": f"Автоматически подтверждаются <b>только выбранные лоты</b> ({len(selected_items)} шт.). Остальные не подтверждаются автоматически.",
        "categories": f"Автоматически подтверждаются <b>только выбранные категории</b> ({len(selected_categories)} шт.). Остальные не подтверждаются автоматически.",
    }.get(mode, "")

    await edit(
        call,
        "✅ <b>Автоподтверждение сделок</b>\n\n"
        "Переводит новые оплаченные заказы (PAID/PENDING) в статус SENT.\n\n"
        f"Текущий режим: <b>{html.escape(mode_label)}</b>\n"
        f"ℹ️ {desc_mode}",
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


@router.callback_query(F.data.startswith("autoconfirm:set_mode:"))
async def autoconfirm_set_mode(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    new_mode = call.data.split(":", 2)[2]
    if new_mode not in ("all", "items", "categories"):
        return
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        cfg = settings(row)
        cfg["auto_confirm_mode"] = new_mode
        row.settings = copy.deepcopy(cfg)
        await session.commit()
        account.settings = cfg
    await call.answer(f"Режим переключен: {new_mode}")
    await render_autoconfirm(call, account)


@router.callback_query(F.data.startswith("autoconfirm:items:"))
async def autoconfirm_items_view(call: CallbackQuery, page_idx: int | None = None) -> None:
    account = await require_account(call)
    if not account:
        return

    if page_idx is None:
        parts = call.data.split(":")
        page_idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    cfg = settings(account)
    selected_items = set(cfg.get("auto_confirm_items") or [])

    try:
        client = await svc().gateway.get_client(account)
        res = await client.call("get_my_items", statuses=None, count=24)
        all_items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        await call.answer(f"Ошибка загрузки: {str(exc)[:100]}", show_alert=True)
        return

    page_size = 6
    total_pages = max(1, (len(all_items) + page_size - 1) // page_size)
    page_idx = max(0, min(page_idx, total_pages - 1))
    page_items = all_items[page_idx * page_size : (page_idx + 1) * page_size]

    b = InlineKeyboardBuilder()
    for item in page_items:
        iid = str(item.id)
        is_sel = iid in selected_items
        icon = "✅ " if is_sel else "▫️ "
        b.button(
            text=f"{icon}{clip(item.name, 22)}",
            callback_data=f"autoconfirm:itoggle:{iid}:{page_idx}",
        )
    b.adjust(1)

    nav = []
    if page_idx > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"autoconfirm:items:{page_idx - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page_idx + 1}/{total_pages}", callback_data="noop"))
    if page_idx < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"autoconfirm:items:{page_idx + 1}"))
    if nav:
        b.row(*nav)

    b.row(
        InlineKeyboardButton(text="✅ Выбрать все", callback_data="autoconfirm:items_all"),
        InlineKeyboardButton(text="❌ Сбросить", callback_data="autoconfirm:items_clear"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:autoconfirm"))

    await call.answer()
    await edit(
        call,
        "🎯 <b>Выбор лотов для автовыполнения</b>\n\n"
        "Отметьте товары, которые бот должен подтверждать автоматически.\n"
        "<i>Невыбранные лоты (например, Telegram) подтверждаться не будут!</i>",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("autoconfirm:itoggle:"))
async def autoconfirm_item_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    parts = call.data.split(":")
    item_id = parts[2]
    page_idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        cfg = settings(row)
        items_list = list(cfg.get("auto_confirm_items") or [])
        if item_id in items_list:
            items_list.remove(item_id)
        else:
            items_list.append(item_id)
        cfg["auto_confirm_items"] = items_list
        row.settings = copy.deepcopy(cfg)
        await session.commit()
        account.settings = cfg

    await call.answer("Сохранено")
    await autoconfirm_items_view(call, page_idx=page_idx)


@router.callback_query(F.data == "autoconfirm:items_all")
async def autoconfirm_items_select_all(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    try:
        client = await svc().gateway.get_client(account)
        res = await client.call("get_my_items", statuses=None, count=24)
        all_items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        await call.answer(f"Ошибка: {str(exc)[:100]}", show_alert=True)
        return

    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        cfg = settings(row)
        cfg["auto_confirm_items"] = [str(i.id) for i in all_items]
        row.settings = copy.deepcopy(cfg)
        await session.commit()
        account.settings = cfg

    await call.answer("Выбраны все лоты")
    await autoconfirm_items_view(call, page_idx=0)


@router.callback_query(F.data == "autoconfirm:items_clear")
async def autoconfirm_items_clear(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        cfg = settings(row)
        cfg["auto_confirm_items"] = []
        row.settings = copy.deepcopy(cfg)
        await session.commit()
        account.settings = cfg

    await call.answer("Список очищен")
    await autoconfirm_items_view(call, page_idx=0)


@router.callback_query(F.data == "autoconfirm:categories")
async def autoconfirm_categories_view(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return

    cfg = settings(account)
    selected_cats = set(cfg.get("auto_confirm_categories") or [])

    try:
        client = await svc().gateway.get_client(account)
        res = await client.call("get_my_items", statuses=None, count=24)
        all_items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        await call.answer(f"Ошибка загрузки: {str(exc)[:100]}", show_alert=True)
        return

    # Извлекаем категории, которые выставлены прямо сейчас
    categories_map: dict[str, str] = {}
    for it in all_items:
        cat = getattr(it, "category", None)
        if cat:
            cid = str(getattr(cat, "id", "") or "")
            cname = str(getattr(cat, "name", "") or "")
            if cid and cname:
                categories_map[cid] = cname

    if not categories_map:
        await call.answer("У вас нет активных категорий", show_alert=True)
        await render_autoconfirm(call, account)
        return

    b = InlineKeyboardBuilder()
    for cid, cname in categories_map.items():
        is_sel = cid in selected_cats or cname in selected_cats
        icon = "✅ " if is_sel else "▫️ "
        b.button(
            text=f"{icon}{clip(cname, 24)}",
            callback_data=f"autoconfirm:ctoggle:{cid}",
        )
    b.adjust(1)

    b.row(
        InlineKeyboardButton(text="✅ Выбрать все", callback_data="autoconfirm:cats_all"),
        InlineKeyboardButton(text="❌ Сбросить", callback_data="autoconfirm:cats_clear"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:autoconfirm"))

    await call.answer()
    await edit(
        call,
        "🗂 <b>Категории для автовыполнения</b>\n\n"
        "Выберите категории из тех, которые выставлены у вас прямо сейчас:\n"
        "<i>Остальные невыбранные категории автоматически выполняться не будут.</i>",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("autoconfirm:ctoggle:"))
async def autoconfirm_category_toggle(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    cat_id = call.data.split(":", 2)[2]

    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        cfg = settings(row)
        cats_list = list(cfg.get("auto_confirm_categories") or [])
        if cat_id in cats_list:
            cats_list.remove(cat_id)
        else:
            cats_list.append(cat_id)
        cfg["auto_confirm_categories"] = cats_list
        row.settings = copy.deepcopy(cfg)
        await session.commit()
        account.settings = cfg

    await call.answer("Сохранено")
    await autoconfirm_categories_view(call)


@router.callback_query(F.data == "autoconfirm:cats_all")
async def autoconfirm_cats_all(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    try:
        client = await svc().gateway.get_client(account)
        res = await client.call("get_my_items", statuses=None, count=24)
        all_items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        await call.answer(f"Ошибка: {str(exc)[:100]}", show_alert=True)
        return

    cats = []
    for it in all_items:
        cat = getattr(it, "category", None)
        if cat:
            cid = str(getattr(cat, "id", "") or "")
            if cid and cid not in cats:
                cats.append(cid)

    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        cfg = settings(row)
        cfg["auto_confirm_categories"] = cats
        row.settings = copy.deepcopy(cfg)
        await session.commit()
        account.settings = cfg

    await call.answer("Выбраны все категории")
    await autoconfirm_categories_view(call)


@router.callback_query(F.data == "autoconfirm:cats_clear")
async def autoconfirm_cats_clear(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        cfg = settings(row)
        cfg["auto_confirm_categories"] = []
        row.settings = copy.deepcopy(cfg)
        await session.commit()
        account.settings = cfg

    await call.answer("Список очищен")
    await autoconfirm_categories_view(call)


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
    account_id = data.get("account_id")
    try:
        account_uuid = uuid.UUID(str(account_id))
    except (TypeError, ValueError):
        await state.clear()
        await message.answer("⚠️ Сессия устарела. Начните добавление правила заново.", reply_markup=main_menu())
        return
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, account_uuid)
        if not account or account.tg_user_id != message.from_user.id:
            await state.clear()
            return
        session.add(AutoReplyRule(
            account_id=account.id,
            trigger=data.get("trigger") or "*",
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
    try:
        account_id = uuid.UUID(str(data.get("account_id")))
    except (TypeError, ValueError):
        await state.clear()
        await message.answer("⚠️ Сессия устарела. Начните настройку автовыдачи заново.", reply_markup=main_menu())
        return
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
        [InlineKeyboardButton(text="📋 Мои товары", callback_data="items:list"),
         InlineKeyboardButton(text="📝 Черновики", callback_data="items:list:drafts")],
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
    payload = {key: value for key, value in row.payload.items() if key != "data_field_values"}
    await state.update_data(account_id=str(account.id), template=True, **payload)
    await _load_category_and_start_options(
        call, state, payload.get("obtaining_id"), payload.get("obtaining_name") or "Стандартный"
    )


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
    try:
        account_uuid = uuid.UUID(str(data.get("account_id")))
    except (TypeError, ValueError):
        await state.clear()
        await message.answer("⚠️ Сессия устарела. Начните создание товара заново.", reply_markup=main_menu())
        return
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, account_uuid)
    if not account or account.tg_user_id != message.from_user.id:
        await state.clear()
        await message.answer("⚠️ Аккаунт не найден. Начните заново.", reply_markup=main_menu())
        return
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
    categories = data.get("categories") or []
    if not (0 <= idx < len(categories)):
        await state.clear()
        await call.answer("Список категорий устарел. Начните заново.", show_alert=True)
        return
    category_id, category_name = categories[idx]
    try:
        account_uuid = uuid.UUID(str(data.get("account_id")))
    except (TypeError, ValueError):
        await state.clear()
        await call.answer("Сессия устарела. Начните заново.", show_alert=True)
        return
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, account_uuid)
    if not account or account.tg_user_id != call.from_user.id:
        await state.clear()
        await call.answer("Аккаунт не найден.", show_alert=True)
        return
    try:
        client = await svc().gateway.get_client(account)
        page = await client.call("get_game_category_obtaining_types", category_id, count=24)
        values = [(str(x.id), str(x.name)) for x in list(getattr(page, "obtaining_types", []) or [])]
    except Exception as exc:
        await call.answer("Ошибка API", show_alert=True)
        await call.message.answer(f"<code>{html.escape(str(exc))[:1200]}</code>", parse_mode="HTML")
        return

    await state.update_data(category_id=category_id, category_name=category_name, obtaining=values)

    if not values:
        # Для категорий без способов получения (например, "Другое" в Claude / ChatGPT)
        await _load_category_and_start_options(call, state, obtaining_id=None, obtaining_name="Стандартный")
        return

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
    obtaining = data.get("obtaining") or []
    if not (0 <= idx < len(obtaining)):
        await state.clear()
        await call.answer("Список способов получения устарел. Начните заново.", show_alert=True)
        return
    obtaining_id, obtaining_name = obtaining[idx]
    await _load_category_and_start_options(call, state, obtaining_id=obtaining_id, obtaining_name=obtaining_name)


async def _load_category_and_start_options(
    call: CallbackQuery,
    state: FSMContext,
    obtaining_id: str | None,
    obtaining_name: str
) -> None:
    data = await state.get_data()
    try:
        account_uuid = uuid.UUID(str(data.get("account_id")))
    except (TypeError, ValueError):
        await state.clear()
        await call.answer("Сессия устарела. Начните заново.", show_alert=True)
        return
    if not data.get("category_id"):
        await state.clear()
        await call.answer("Категория не выбрана. Начните заново.", show_alert=True)
        return
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, account_uuid)
    if not account or account.tg_user_id != call.from_user.id:
        await state.clear()
        await call.answer("Аккаунт не найден.", show_alert=True)
        return
    try:
        client = await svc().gateway.get_client(account)
        category = await client.call("get_game_category", id=data["category_id"])

        fields = await get_item_data_fields(client, data["category_id"], obtaining_id)
    except Exception as exc:
        await call.answer("Ошибка API", show_alert=True)
        await call.message.answer(f"<code>{html.escape(str(exc))[:1200]}</code>", parse_mode="HTML")
        return

    options = list(getattr(category, "options", []) or [])
    message_field_id = buyer_message_field_id(fields)
    field_meta = [
        (str(field.id), str(field.label), bool(field.required))
        for field in fields if str(field.id) != message_field_id
    ]

    # Группируем опции по их полю и названию группы с сохранением типа (RANGE или SELECTOR)
    groups_dict: dict[str, dict[str, Any]] = {}
    for o in options:
        fld = str(o.field)
        grp = str(o.group or fld)
        opt_type = getattr(o, "type", None)
        type_name = getattr(opt_type, "name", str(opt_type or "SELECTOR"))
        range_limit = getattr(o, "value_range_limit", None)
        min_val = getattr(range_limit, "min", None) if range_limit else None
        max_val = getattr(range_limit, "max", None) if range_limit else None

        if fld not in groups_dict:
            groups_dict[fld] = {
                "field": fld,
                "group_name": grp,
                "type": type_name,
                "min": min_val,
                "max": max_val,
                "multiple": bool(getattr(o, "multiple", False)),
                "choices": [],
            }
        groups_dict[fld]["choices"].append((str(o.value), str(o.label)))
        if min_val is not None:
            groups_dict[fld]["min"] = min_val
        if max_val is not None:
            groups_dict[fld]["max"] = max_val
        if type_name == "RANGE":
            groups_dict[fld]["type"] = "RANGE"

    option_groups = list(groups_dict.values())

    await state.update_data(
        obtaining_id=obtaining_id,
        obtaining_name=obtaining_name,
        field_meta=field_meta,
        buyer_message_field_id=message_field_id,
        option_groups=option_groups,
        attribute_group_index=0,
        attributes=data.get("attributes", {}) if data.get("template") else {},
    )

    if option_groups and not data.get("template"):
        await _show_attribute_group(call, state)
    else:
        await call.answer()
        await _start_item_fields(call.message, state)


PAGE_SIZE_OPTIONS = 15


async def _show_attribute_group(call_or_msg: Any, state: FSMContext, page: int = 0) -> None:
    data = await state.get_data()
    groups = data.get("option_groups") or []
    idx = int(data.get("attribute_group_index", 0))
    if idx >= len(groups):
        target = call_or_msg.message if isinstance(call_or_msg, CallbackQuery) else call_or_msg
        await _start_item_fields(target, state)
        return

    grp = groups[idx]
    group_name = grp["group_name"]
    grp_type = grp.get("type", "SELECTOR")
    choices = grp.get("choices", [])

    b = InlineKeyboardBuilder()

    if grp_type == "RANGE":
        min_v = grp.get("min")
        max_v = grp.get("max")
        range_hint = ""
        if min_v is not None and max_v is not None:
            range_hint = f" (от {min_v} до {max_v})"
        elif min_v is not None:
            range_hint = f" (от {min_v})"

        prompt = (
            f"⚙️ <b>Параметр {idx + 1}/{len(groups)}: {html.escape(group_name)}</b>\n\n"
            f"Введите числовое значение{range_hint} сообщением в чат:\n"
            f"<i>(Или нажмите кнопку ниже, чтобы пропустить)</i>"
        )
        b.button(text="⏭ Пропустить этот параметр", callback_data=f"itemopt:{idx}:skip")
        b.adjust(1)
    else:
        total_choices = len(choices)
        total_pages = max(1, (total_choices + PAGE_SIZE_OPTIONS - 1) // PAGE_SIZE_OPTIONS)
        page = max(0, min(page, total_pages - 1))
        start_i = page * PAGE_SIZE_OPTIONS
        end_i = start_i + PAGE_SIZE_OPTIONS
        page_choices = choices[start_i:end_i]

        for rel_i, (_val, label) in enumerate(page_choices):
            real_i = start_i + rel_i
            b.button(text=clip(label, 36), callback_data=f"itemopt:{idx}:{real_i}")
        b.adjust(1)

        # Пагинация для длинных списков серверов / атрибутов
        if total_pages > 1:
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"itemoptpage:{idx}:{page - 1}"))
            nav_row.append(InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="noop"))
            if page + 1 < total_pages:
                nav_row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"itemoptpage:{idx}:{page + 1}"))
            b.row(*nav_row)

        b.row(InlineKeyboardButton(text="⏭ Пропустить этот атрибут", callback_data=f"itemopt:{idx}:skip"))

        page_info = f" (стр. {page + 1}/{total_pages})" if total_pages > 1 else ""
        prompt = (
            f"⚙️ <b>Параметр {idx + 1}/{len(groups)}: {html.escape(group_name)}</b>{page_info}\n"
            "Выберите подходящее значение:"
        )

    await state.set_state(ItemCreate.attributes)
    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.answer()
        await edit(call_or_msg, prompt, b.as_markup())
    else:
        await call_or_msg.answer(prompt, reply_markup=b.as_markup(), parse_mode="HTML")


def _format_field_prompt(field: tuple[str, str, bool], index: int, count: int) -> str:
    _fid, label, required = field
    requirement = "<b>Обязательное поле.</b>" if required else "Отправьте «-», чтобы пропустить."
    return f"🔐 <b>Данные товара {index}/{count}</b>\n\n{html.escape(label)}\n{requirement}"


async def _start_item_fields(target: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data.get("field_meta") or []
    await state.update_data(field_index=0, data_field_values={}, buyer_message=None)
    if not fields:
        await _ask_buyer_message(target, state)
        return
    await state.set_state(ItemCreate.data_fields)
    prompt = _format_field_prompt(fields[0], 1, len(fields))
    await target.answer(prompt, parse_mode="HTML")


@router.callback_query(ItemCreate.attributes, F.data.startswith("itemoptpage:"))
async def item_option_page(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        grp_idx = int(parts[1])
        page = int(parts[2])
        await state.update_data(attribute_group_index=grp_idx)
        await _show_attribute_group(call, state, page=page)
    else:
        await call.answer()


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


@router.message(ItemCreate.attributes)
async def item_attribute_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    groups = data.get("option_groups") or []
    idx = int(data.get("attribute_group_index", 0))

    if idx >= len(groups):
        await _start_item_fields(message, state)
        return

    grp = groups[idx]
    grp_type = grp.get("type", "SELECTOR")
    text = (message.text or "").strip()

    if text in {"-", "⏭", "пропустить", "skip"}:
        next_idx = idx + 1
        await state.update_data(attribute_group_index=next_idx)
        if next_idx < len(groups):
            await _show_attribute_group(message, state)
        else:
            await _start_item_fields(message, state)
        return

    if grp_type == "RANGE":
        clean_text = text.replace(" ", "").replace("_", "").replace(",", ".")
        try:
            val_num = int(float(clean_text))
        except ValueError:
            await message.answer("⚠️ Введите целое число (или нажмите «Пропустить»):")
            return

        min_v = grp.get("min")
        max_v = grp.get("max")
        if min_v is not None and val_num < min_v:
            await message.answer(f"⚠️ Минимальное значение: {min_v}. Введите число не меньше {min_v}:")
            return
        if max_v is not None and val_num > max_v:
            await message.answer(f"⚠️ Максимальное значение: {max_v}. Введите число не больше {max_v}:")
            return

        attributes = dict(data.get("attributes") or {})
        attributes[grp["field"]] = val_num
        next_idx = idx + 1
        await state.update_data(attributes=attributes, attribute_group_index=next_idx)
        if next_idx < len(groups):
            await _show_attribute_group(message, state)
        else:
            await _start_item_fields(message, state)
    else:
        # Для текстового ввода строковых атрибутов, если введен вручную
        attributes = dict(data.get("attributes") or {})
        attributes[grp["field"]] = text
        next_idx = idx + 1
        await state.update_data(attributes=attributes, attribute_group_index=next_idx)
        if next_idx < len(groups):
            await _show_attribute_group(message, state)
        else:
            await _start_item_fields(message, state)


@router.message(ItemCreate.data_fields, F.text)
async def item_fields(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data.get("field_meta") or []
    index = int(data.get("field_index", 0))
    if index >= len(fields):
        await _start_item_fields(message, state)
        return
    field_id, label, required = fields[index]
    raw_value = (message.text or "").strip()
    skipped = raw_value.casefold() in SKIP_VALUES
    if required and skipped:
        await message.answer(f"⚠️ Поле «{html.escape(label)}» <b>обязательно</b> для выставления этого лота. Пожалуйста, введите корректное значение:", parse_mode="HTML")
        return
    values = dict(data.get("data_field_values") or {})
    if not skipped:
        values[field_id] = raw_value
    index += 1
    if index < len(fields):
        await state.update_data(data_field_values=values, field_index=index)
        prompt = _format_field_prompt(fields[index], index + 1, len(fields))
        await message.answer(prompt, parse_mode="HTML")
        return
    await state.update_data(data_field_values=values)
    await _ask_buyer_message(message, state)


async def _ask_buyer_message(target: Message, state: FSMContext) -> None:
    await state.set_state(ItemCreate.buyer_message)
    await target.answer(
        BUYER_MESSAGE_PROMPT + "Введите обязательный текст, который Playerok покажет покупателю в сделке.",
        parse_mode="HTML",
    )


@router.message(ItemCreate.buyer_message, F.text)
async def item_create_buyer_message(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not valid_buyer_message(text):
        await message.answer(BUYER_MESSAGE_ERROR)
        return
    await state.update_data(buyer_message=text)
    data = await state.get_data()
    if data.get("template") or all(data.get(key) for key in ("item_name", "item_price", "item_description")):
        await _ask_item_images(message, state, keep_photos=True)
        return
    await state.set_state(ItemCreate.name)
    await message.answer("🏷 <b>Название товара</b>", parse_mode="HTML")


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
        if value <= 0 or value > 10_000_000:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите положительное целое число от 1 до 10 000 000 ₽:")
        return
    await state.update_data(item_price=value)
    # Скидка теперь задается в самом конце перед публикацией или при редактировании лота
    await state.set_state(ItemCreate.description)
    await message.answer(f"💰 Цена: <b>{value} ₽</b>\n\n📝 <b>Описание товара:</b>", parse_mode="HTML")


@router.message(ItemCreate.description, F.text)
async def item_description(message: Message, state: FSMContext) -> None:
    description = message.text.strip()
    if not description:
        await message.answer("Введите описание товара.")
        return
    await state.update_data(item_description=description)
    await _ask_item_images(message, state)


async def _ask_item_images(message: Message, state: FSMContext, *, keep_photos: bool = False) -> None:
    if not keep_photos:
        await state.update_data(attachments_b64=[])
    await state.set_state(ItemCreate.image)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Продолжить к проверке лота", callback_data="itemimage:skip")]
    ])
    await message.answer(
        "📸 <b>Фотографии товара</b>\n\nОтправьте до 10 фотографий и нажмите «Продолжить».",
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
        await message.answer("Достигнут лимит 10 фотографий. Нажмите «Продолжить» под добавленным фото.")
        return

    buffer = io.BytesIO()
    await bot.download(message.photo[-1], destination=buffer)
    b64_str = base64.b64encode(buffer.getvalue()).decode()
    current_list.append(b64_str)
    await state.update_data(attachments_b64=current_list)

    count = len(current_list)
    b = InlineKeyboardBuilder()
    b.button(text=f"Продолжить ({count} фото)", callback_data="itemimage:done")
    b.adjust(1)

    await message.answer(
        f"📸 <b>Фото #{count} добавлено!</b>\n\n"
        "Вы можете отправить следующее фото или завершить создание лота:",
        reply_markup=b.as_markup(),
        parse_mode="HTML",
    )


async def create_draft(target: Message, state: FSMContext, tg_id: int) -> None:
    data = await state.get_data()
    if any(not data.get(key) for key in ("account_id", "category_id", "item_name", "item_price", "item_description")):
        await target.answer("Заполните название, цену и описание товара.", reply_markup=main_menu())
        return

    account = await _draft_account(data, tg_id)
    if not account:
        await state.clear()
        await target.answer("Аккаунт не найден. Начните создание товара заново.", reply_markup=main_menu())
        return

    buyer_message = data.get("buyer_message") or ""
    if not valid_buyer_message(buyer_message):
        await _ask_buyer_message(target, state)
        return

    values = data.get("data_field_values") or {}
    comment, fields = item_delivery_payload(buyer_message, data.get("buyer_message_field_id"), values)
    wait_msg = await target.answer("Создаю черновик на Playerok…")
    try:
        client = await svc().gateway.get_client(account)
        item = await client.create_item(
            game_category_id=data["category_id"],
            obtaining_type_id=data.get("obtaining_id"),
            name=data["item_name"],
            price=data["item_price"],
            description=data["item_description"],
            options=data.get("attributes") or {},
            data_fields=fields,
            attachments=[base64.b64decode(photo) for photo in data.get("attachments_b64", [])],
            comment=comment,
        )
        if item is None or not item.id:
            raise ValueError("Missing Playerok draft")
        prices = int(item.price), tariff_price(item)
    except Exception as exc:
        logger.warning("Playerok draft creation failed (%s)", type(exc).__name__)
        detail = (
            "Playerok не подтвердил создание черновика.\n"
            f"<code>{html.escape(playerok_error_text(exc))}</code>\n"
            "Перед повтором проверьте список черновиков."
        )
        await wait_msg.edit_text(
            detail,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Повторить", callback_data="itemimage:done")],
                [InlineKeyboardButton(text="Черновики", callback_data="items:list:drafts")],
            ]),
        )
        return

    await state.update_data(
        new_item_id=str(item.id), item_sale_price=prices[0], item_price=prices[1],
        attachments_b64=[], data_field_values={}, buyer_message=None,
    )
    await state.set_state(ItemCreate.priority)
    try:
        await wait_msg.delete()
    except TelegramBadRequest:
        pass
    await _show_draft_review(target, await state.get_data())


async def _draft_account(data: dict[str, Any], tg_id: int) -> PlayerokAccount | None:
    try:
        account_id = uuid.UUID(str(data.get("account_id")))
    except (TypeError, ValueError):
        return None
    async with svc().db() as session:
        account = await session.get(PlayerokAccount, account_id)
    return account if account and account.tg_user_id == tg_id else None


async def _show_draft_review(target: CallbackQuery | Message, data: dict[str, Any]) -> None:
    item_id = data["new_item_id"]
    price = data.get("item_sale_price", data["item_price"])
    base = data["item_price"]
    price_line = f"<s>{base} ₽</s> → <b>{price} ₽</b>" if price < base else f"<b>{price} ₽</b>"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выбрать способ публикации", callback_data=f"item:publish:{item_id}")],
        [InlineKeyboardButton(text="Изменить скидку" if price < base else "Добавить скидку", callback_data="itemdiscount:prompt")],
        [InlineKeyboardButton(text="Сохранить в черновиках", callback_data="itemprio:draft")],
        [InlineKeyboardButton(text="Сохранить как шаблон", callback_data="itemtemplate:save")],
    ])
    text = (
        f"📝 <b>Черновик · {html.escape(data['item_name'])}</b>\n\n"
        f"Цена продажи: {price_line}\n"
        "Комментарий покупателю сохранён в лоте Playerok."
    )
    if isinstance(target, CallbackQuery):
        await edit(target, text, markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(ItemCreate.priority, F.data == "itemdraft:review")
async def item_draft_review(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.update_data(publication=None)
    await _show_draft_review(call, await state.get_data())


@router.callback_query(ItemCreate.priority, F.data == "itemdiscount:prompt")
async def item_create_discount_prompt(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    base_price = data["item_price"]
    await state.update_data(publication=None)
    await state.set_state(ItemCreate.discount_price)
    await call.answer()
    await edit(
        call,
        "🔥 <b>Скидка до публикации</b>\n\n"
        f"Базовая цена: <b>{base_price} ₽</b>. Введите цену со скидкой от 5% "
        "или отправьте «-», чтобы снять скидку.",
        back_menu("itemdiscount:cancel"),
    )


@router.callback_query(ItemCreate.discount_price, F.data == "itemdiscount:cancel")
async def item_create_discount_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(ItemCreate.priority)
    await _show_draft_review(call, await state.get_data())


@router.message(ItemCreate.discount_price, F.text)
async def item_create_discount_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    base_price = data["item_price"]
    text = message.text.strip()
    try:
        price = parse_discount_price(text, base_price)
    except ValueError:
        await message.answer(f"Введите целую цену от 1 до {base_price * 95 // 100} ₽ или «-» для снятия скидки.")
        return

    try:
        account = await _draft_account(data, message.from_user.id)
        if not account:
            await state.clear()
            await message.answer("Аккаунт не найден.", reply_markup=main_menu())
            return
        client = await svc().gateway.get_client(account)
        current = await client.get_item(data["new_item_id"])
        if getattr(current.status, "name", current.status) != "DRAFT" or tariff_price(current) != base_price:
            await state.clear()
            await message.answer(
                "Статус или базовая цена изменились. Откройте товар заново.",
                reply_markup=back_menu(f"item:view:{data['new_item_id']}"),
            )
            return
        item = await client.update_item(data["new_item_id"], price=price)
    except Exception as exc:
        await message.answer(f"Playerok не принял изменение цены:\n<code>{html.escape(clip(str(exc), 350))}</code>", parse_mode="HTML")
        return

    await state.update_data(item_sale_price=int(item.price), item_price=tariff_price(item), publication=None)
    await state.set_state(ItemCreate.priority)
    await _show_draft_review(message, await state.get_data())


@router.callback_query(ItemCreate.priority, F.data == "itemtemplate:save")
async def item_template_save_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(publication=None)
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
    account_id = data.get("account_id")
    if not account_id:
        await state.clear()
        await message.answer("⚠️ Сессия устарела. Начните создание товара заново.", reply_markup=main_menu())
        return
    payload = {
        key: data[key]
        for key in (
            "category_id", "category_name", "obtaining_id", "obtaining_name", "attributes",
            "item_name", "item_price", "item_description",
        )
        if key in data
    }
    try:
        account_uuid = uuid.UUID(str(account_id))
    except (TypeError, ValueError):
        await state.clear()
        await message.answer("⚠️ Некорректная сессия. Начните создание товара заново.", reply_markup=main_menu())
        return
    async with svc().db() as session:
        session.add(ItemTemplate(account_id=account_uuid, name=name, payload=payload))
        await session.commit()
    await state.clear()
    await message.answer("✅ Шаблон сохранён.", reply_markup=main_menu())


@router.callback_query(ItemCreate.priority, F.data == "itemprio:draft")
async def leave_draft(call: CallbackQuery, state: FSMContext) -> None:
    item_id = (await state.get_data())["new_item_id"]
    await state.clear()
    await call.answer("Черновик сохранён")
    await edit(call, "Черновик сохранён на Playerok.", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть черновик", callback_data=f"item:view:{item_id}")],
        [InlineKeyboardButton(text="Мои товары", callback_data="items:list")],
    ]))


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Используйте меню ниже.", reply_markup=main_menu())
