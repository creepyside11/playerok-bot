from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, data in [
        ("👤 Профиль", "menu:profile"),
        ("💰 Баланс", "menu:balance"),
        ("💬 Чаты", "menu:chats"),
        ("📦 Сделки и заказы", "menu:deals"),
        ("📢 Товары", "menu:items"),
        ("⚡ Автовыдача", "menu:delivery"),
        ("🤖 Автоответчик", "menu:autoreply"),
        ("✅ Автоподтверждение", "menu:autoconfirm"),
        ("🧩 Плагины", "menu:plugins"),
        ("🔔 Уведомления", "menu:notifications"),
        ("🔐 Аккаунты", "menu:accounts"),
    ]:
        b.button(text=text, callback_data=data)
    b.adjust(2)
    return b.as_markup()


def back_menu(target: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"menu:{target}")]]
    )


def auth_method_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍪 Cookie", callback_data="auth:cookie")],
        [InlineKeyboardButton(text="📧 Почта + код", callback_data="auth:email")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow:cancel")],
    ])


def delivery_mode_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Постоянный текст", callback_data="delivery_mode:static")],
        [InlineKeyboardButton(text="🔑 Склад (1 строка = 1 выдача)", callback_data="delivery_mode:stock")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow:cancel")],
    ])
