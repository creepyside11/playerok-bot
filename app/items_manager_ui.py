from __future__ import annotations

import html
import io
import logging
import secrets
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .handlers import active_account, clip, edit, require_account, svc
from playerokapi.enums import ItemStatuses

from .item_delivery import (
    BUYER_MESSAGE_ERROR, BUYER_MESSAGE_PROMPT, ListingFieldsUnavailable,
    listing_comment_update, valid_buyer_message,
)
from .item_publication import (
    PublicationChanged, PublicationUnconfirmed, can_publish, confirm_publication, parse_discount_price,
    publication_balance,
    publication_tariffs, rubles, tariff_price,
)
from .keyboards import back_menu
from .playerok_errors import playerok_error_text, strip_error_detail
from .states import ItemCreate, ItemEdit

logger = logging.getLogger("items_manager")
router = Router(name="items_manager")

PAGE_SIZE = 8


def item_status_badge(status: Any) -> str:
    raw = str(getattr(status, "name", status) or "").upper()
    if raw in ("APPROVED", "ACTIVE"):
        return "🟢 На продаже"
    elif raw in ("PENDING", "MODERATION", "ON_MODERATION", "PENDING_APPROVAL", "PENDING_MODERATION"):
        return "⏳ На модерации"
    elif raw in ("SOLD", "COMPLETED"):
        return "📦 Продан"
    elif raw in ("PAUSED", "DISABLED", "ARCHIVED"):
        return "⏸ Снят с продажи"
    elif raw == "DRAFT":
        return "📝 Черновик"
    return f"ℹ️ {html.escape(raw)}"


@router.callback_query(F.data == "items:list")
@router.callback_query(F.data.startswith("items:list:"))
async def items_list_view(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    parts = call.data.split(":")
    drafts = "drafts" in parts
    page_idx = min(int(parts[-1]), 1000) if parts[-1].isdigit() else 0
    list_callback = "items:list:drafts" if drafts else "items:list"

    await call.answer("Загружаю товары…")
    try:
        client = await svc().gateway.get_client(account)
        cursor = None
        for idx in range(page_idx + 1):
            page = await client.call(
                "get_my_items", statuses=[ItemStatuses.DRAFT] if drafts else None,
                count=PAGE_SIZE, after_cursor=cursor,
            )
            page_info = getattr(page, "page_info", None)
            next_cursor = getattr(page_info, "end_cursor", None)
            if not getattr(page_info, "has_next_page", False) or not next_cursor or next_cursor == cursor:
                page_idx = idx
                break
            cursor = next_cursor
        current_items = [item for item in page.items if item]
        total_count = page.total_count
        has_next = bool(getattr(page_info, "has_next_page", False) and next_cursor)
    except Exception as exc:
        await edit(
            call,
            f"❌ <b>Ошибка при загрузке списка товаров:</b>\n\n<code>{html.escape(str(exc))[:1500]}</code>",
            back_menu("items"),
        )
        return

    builder = InlineKeyboardBuilder()
    for item in current_items:
        badge = item_status_badge(item.status).split(" ", 1)[0]
        builder.button(
            text=f"{badge} {item.price} ₽ · {clip(item.name, 35)}",
            callback_data=f"item:view:{item.id}",
        )

    builder.adjust(1)

    # Пагинация
    nav_buttons = []
    if page_idx > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{list_callback}:{page_idx - 1}"))
    if has_next:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"{list_callback}:{page_idx + 1}"))
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        InlineKeyboardButton(text="Выставить товар", callback_data="catalog:search"),
        InlineKeyboardButton(text="Обновить", callback_data=f"{list_callback}:{page_idx}"),
    )
    builder.row(
        InlineKeyboardButton(
            text="Все товары" if drafts else "Черновики",
            callback_data="items:list" if drafts else "items:list:drafts",
        ),
        InlineKeyboardButton(text="Меню товаров", callback_data="menu:items"),
    )
    title = "Черновики" if drafts else "Мои товары"
    text = f"📋 <b>{title} · {html.escape(account.username or '')}</b>"
    if current_items:
        total = f" · всего {total_count}" if total_count is not None else ""
        text += f"\nСтраница {page_idx + 1}{total}"
    else:
        text += "\nЗдесь пока пусто."
    await edit(call, text, builder.as_markup())


async def render_item_card(target: CallbackQuery | Message, account: Any, client: Any, item_id: str) -> None:
    try:
        item = await client.get_item(item_id)
    except Exception as exc:
        err_text = f"❌ Не удалось загрузить товар <code>{html.escape(item_id)}</code>:\n<code>{html.escape(str(exc))[:1500]}</code>"
        if isinstance(target, CallbackQuery):
            await edit(target, err_text, back_menu("items:list"))
        else:
            await target.answer(err_text, parse_mode="HTML", reply_markup=back_menu("items:list"))
        return

    name = str(getattr(item, "name", "Без названия") or "Без названия")
    price = getattr(item, "price", 0)
    base_price = tariff_price(item)
    desc = str(getattr(item, "description", "") or "").strip()
    status = getattr(item, "status", None)
    status_label = item_status_badge(status)
    attachments = list(getattr(item, "attachments", []) or [])
    game = getattr(item, "game", None)
    game_name = str(getattr(game, "name", "—") or "—")
    category = getattr(item, "category", None)
    category_name = str(getattr(category, "name", "—") or "—")

    price_str = f"<b>{price} ₽</b>"
    if base_price > int(price):
        price_str = f"<s>{base_price} ₽</s> → {price_str}"

    desc_display = html.escape(desc)
    if len(desc_display) > 800:
        desc_display = desc_display[:800] + "… <i>[обрезано для превью]</i>"
    elif not desc_display:
        desc_display = "<i>[Описание отсутствует]</i>"

    photos_count = len(attachments)
    photos_note = f"<b>{photos_count} шт.</b>"
    if attachments and hasattr(attachments[0], "url"):
        photos_note += f" · <a href=\"{attachments[0].url}\">Обложка ↗</a>"

    lines = [
        f"📦 <b>Лот: {html.escape(name)}</b>",
        "",
        f"💰 Цена: {price_str}",
        f"📊 Статус: <b>{status_label}</b>",
        f"🎮 Игра / Раздел: <b>{html.escape(game_name)}</b>",
        f"📂 Категория: <b>{html.escape(category_name)}</b>",
        f"🖼 Фотографий: {photos_note}",
        f"🆔 ID: <code>{html.escape(item_id)}</code>",
        "",
        "📝 <b>Описание товара:</b>",
        desc_display,
    ]

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Название", callback_data=f"item:edit:name:{item_id}"),
        InlineKeyboardButton(text="💰 Изменить цену", callback_data=f"item:edit:price:{item_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🔥 Сделать скидку", callback_data=f"item:edit:discount:{item_id}"),
        InlineKeyboardButton(text="📝 Изменить описание", callback_data=f"item:edit:desc:{item_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="✉️ Комментарий покупателю", callback_data=f"item:edit:goods:{item_id}"),
    )
    builder.row(
        InlineKeyboardButton(text=f"🖼 Фото ({photos_count})", callback_data=f"item:edit:photo:{item_id}"),
    )

    if can_publish(item):
        builder.row(InlineKeyboardButton(text="🚀 Опубликовать на Playerok", callback_data=f"item:publish:{item_id}"))

    builder.row(InlineKeyboardButton(text="🗑 Удалить товар", callback_data=f"item:delete:ask:{item_id}"))
    builder.row(
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"item:view:{item_id}"),
        InlineKeyboardButton(text="⬅️ К списку товаров", callback_data="items:list"),
    )

    full_text = "\n".join(lines)
    if isinstance(target, CallbackQuery):
        await edit(target, full_text, builder.as_markup(), disable_web_page_preview=False)
    else:
        await target.answer(full_text, parse_mode="HTML", reply_markup=builder.as_markup(), disable_web_page_preview=False)


@router.callback_query(F.data.startswith("item:view:"))
async def item_view(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[2]
    await call.answer("Загружаю лот…")
    client = await svc().gateway.get_client(account)
    await render_item_card(call, account, client, item_id)


# --- РЕДАКТИРОВАНИЕ НАЗВАНИЯ ---

@router.callback_query(F.data.startswith("item:edit:name:"))
async def item_edit_name_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[3]
    await state.clear()
    await state.set_state(ItemEdit.name)
    await state.update_data(item_id=item_id, account_id=str(account.id))
    await call.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"item:view:{item_id}")]]
    )
    await edit(
        call,
        "✏️ <b>Изменение названия товара</b>\n\n"
        "Отправьте новое название для лота (от 3 до 200 символов).\n\n"
        "Для отмены нажмите кнопку ниже или введите /cancel.",
        markup,
    )


@router.message(ItemEdit.name, F.text)
async def item_save_name(message: Message, state: FSMContext) -> None:
    new_name = (message.text or "").strip()
    if len(new_name) < 3 or len(new_name) > 200:
        await message.answer("⚠️ Название должно быть длиной от 3 до 200 символов. Попробуйте еще раз:")
        return

    data = await state.get_data()
    item_id = data.get("item_id")
    await state.clear()

    account = await active_account(message.from_user.id)
    if not account or not item_id:
        await message.answer("❌ Аккаунт не найден.")
        return

    wait_msg = await message.answer("⏳ Сохраняю новое название в Playerok…")
    try:
        client = await svc().gateway.get_client(account)
        await client.update_item(item_id, name=new_name)
        await wait_msg.delete()
        await message.answer(f"✅ <b>Название товара успешно обновлено:</b>\n«{html.escape(new_name)}»", parse_mode="HTML")
        await render_item_card(message, account, client, item_id)
    except Exception as exc:
        await wait_msg.delete()
        await message.answer(
            f"❌ <b>Ошибка при обновлении названия:</b>\n<code>{html.escape(str(exc))[:1500]}</code>",
            parse_mode="HTML",
            reply_markup=back_menu(f"item:view:{item_id}"),
        )


# --- РЕДАКТИРОВАНИЕ ЦЕНЫ ---

@router.callback_query(F.data.startswith("item:edit:price:"))
async def item_edit_price_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[3]
    await state.clear()
    await state.set_state(ItemEdit.price)
    await state.update_data(item_id=item_id, account_id=str(account.id))
    await call.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"item:view:{item_id}")]]
    )
    await edit(
        call,
        "💰 <b>Изменение цены товара</b>\n\n"
        "Отправьте новую цену в рублях (целое число от 1 до 10 000 000 ₽).\n\n"
        "Для отмены нажмите кнопку ниже или введите /cancel.",
        markup,
    )


@router.message(ItemEdit.price, F.text)
async def item_save_price(message: Message, state: FSMContext) -> None:
    clean_val = (message.text or "").strip().replace(" ", "").replace("₽", "").replace("руб", "").replace("р", "")
    try:
        new_price = int(clean_val)
        if new_price < 1 or new_price > 10_000_000:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите корректное целое число от 1 до 10 000 000 ₽:")
        return

    data = await state.get_data()
    item_id = data.get("item_id")
    await state.clear()

    account = await active_account(message.from_user.id)
    if not account or not item_id:
        await message.answer("❌ Аккаунт не найден.")
        return

    wait_msg = await message.answer("⏳ Обновляю цену в Playerok…")
    try:
        client = await svc().gateway.get_client(account)
        await client.update_item(item_id, price=new_price)
        await wait_msg.delete()
        await message.answer(f"✅ <b>Цена успешно изменена: {new_price} ₽</b>", parse_mode="HTML")
        await render_item_card(message, account, client, item_id)
    except Exception as exc:
        await wait_msg.delete()
        await message.answer(
            f"❌ <b>Ошибка при изменении цены:</b>\n<code>{html.escape(str(exc))[:1500]}</code>",
            parse_mode="HTML",
            reply_markup=back_menu(f"item:view:{item_id}"),
        )


# --- УСТАНОВКА ЦЕНЫ СО СКИДКОЙ ---

@router.callback_query(F.data.startswith("item:edit:discount:"))
async def item_edit_discount_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.rsplit(":", 1)[1]
    await call.answer()
    client = await svc().gateway.get_client(account)
    try:
        item = await client.get_item(item_id)
        base_price = tariff_price(item)
    except Exception:
        await edit(call, "Не удалось загрузить цену товара.", back_menu(f"item:view:{item_id}"))
        return
    await state.clear()
    await state.set_state(ItemEdit.discount)
    await state.update_data(item_id=item_id, account_id=str(account.id), base_price=base_price)
    await edit(
        call,
        "🔥 <b>Скидка на товар</b>\n\n"
        f"Базовая цена: <b>{base_price} ₽</b>. Введите цену со скидкой от 5% "
        "или отправьте «-», чтобы снять скидку.",
        back_menu(f"item:view:{item_id}"),
    )


@router.message(ItemEdit.discount, F.text, ~F.text.startswith("/"))
async def item_save_discount(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    item_id = data["item_id"]
    base_price = data["base_price"]
    account = await active_account(message.from_user.id)
    if not account or data["account_id"] != str(account.id):
        await state.clear()
        await message.answer("Аккаунт изменился. Откройте товар заново.")
        return

    text = message.text.strip()
    try:
        price = parse_discount_price(text, base_price)
    except ValueError:
        await message.answer(f"Введите цену от 1 до {base_price * 95 // 100} ₽ или «-» для снятия скидки.")
        return

    client = await svc().gateway.get_client(account)
    try:
        current = await client.get_item(item_id)
        if tariff_price(current) != base_price:
            await state.clear()
            await message.answer("Базовая цена изменилась. Откройте настройки скидки заново.", reply_markup=back_menu(f"item:view:{item_id}"))
            return
        await client.update_item(item_id, price=price)
    except Exception as exc:
        await message.answer(f"Playerok не принял изменение цены:\n<code>{html.escape(clip(str(exc), 350))}</code>", parse_mode="HTML")
        return
    await state.clear()
    await render_item_card(message, account, client, item_id)


# --- РЕДАКТИРОВАНИЕ ОПИСАНИЯ ---

@router.callback_query(F.data.startswith("item:edit:desc:"))
async def item_edit_desc_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[3]
    await state.clear()
    await state.set_state(ItemEdit.description)
    await state.update_data(item_id=item_id, account_id=str(account.id))
    await call.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"item:view:{item_id}")]]
    )
    await edit(
        call,
        "📝 <b>Изменение описания товара</b>\n\n"
        "Отправьте новый текст описания (до 4000 символов).\n\n"
        "Для отмены нажмите кнопку ниже или введите /cancel.",
        markup,
    )


@router.message(ItemEdit.description, F.text)
async def item_save_description(message: Message, state: FSMContext) -> None:
    new_desc = (message.text or "").strip()
    if not new_desc or len(new_desc) > 5000:
        await message.answer("⚠️ Описание не должно быть пустым и не может превышать 5000 символов. Отправьте текст еще раз:")
        return

    data = await state.get_data()
    item_id = data.get("item_id")
    await state.clear()

    account = await active_account(message.from_user.id)
    if not account or not item_id:
        await message.answer("❌ Аккаунт не найден.")
        return

    wait_msg = await message.answer("⏳ Сохраняю описание в Playerok…")
    try:
        client = await svc().gateway.get_client(account)
        await client.update_item(item_id, description=new_desc)
        await wait_msg.delete()
        await message.answer("✅ <b>Описание товара успешно обновлено!</b>", parse_mode="HTML")
        await render_item_card(message, account, client, item_id)
    except Exception as exc:
        await wait_msg.delete()
        await message.answer(
            f"❌ <b>Ошибка при сохранении описания:</b>\n<code>{html.escape(str(exc))[:1500]}</code>",
            parse_mode="HTML",
            reply_markup=back_menu(f"item:view:{item_id}"),
        )


@router.callback_query(F.data.startswith("item:edit:delivery:"))
@router.callback_query(F.data.startswith("item:edit:goods:"))
async def item_edit_buyer_message_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.rsplit(":", 1)[1]
    await state.clear()
    await state.set_state(ItemEdit.buyer_message)
    await state.update_data(item_id=item_id, account_id=str(account.id))
    await call.answer()
    await edit(
        call,
        BUYER_MESSAGE_PROMPT + "Введите новый текст, который Playerok покажет покупателю в сделке.",
        back_menu(f"item:view:{item_id}"),
    )


@router.message(ItemEdit.buyer_message, F.text, ~F.text.startswith("/"))
async def item_save_buyer_message(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not valid_buyer_message(text):
        await message.answer(BUYER_MESSAGE_ERROR)
        return

    data = await state.get_data()
    account = await active_account(message.from_user.id)
    if not account or data["account_id"] != str(account.id):
        await state.clear()
        await message.answer("Аккаунт изменился. Откройте товар заново.")
        return
    item_id = data["item_id"]
    try:
        client = await svc().gateway.get_client(account)
        item = await client.get_item(item_id)
        patch = await listing_comment_update(client, item, text)
        await client.update_item(item_id, **patch)
    except ListingFieldsUnavailable as exc:
        markup = InlineKeyboardBuilder()
        if item.slug:
            markup.button(text="Редактировать на Playerok", url=f"https://playerok.com/products/{item.slug}/edit")
        markup.button(text="Назад к лоту", callback_data=f"item:view:{item_id}")
        markup.adjust(1)
        await message.answer(str(exc), reply_markup=markup.as_markup())
        return
    except Exception as exc:
        await message.answer(f"Не удалось сохранить комментарий.\n{playerok_error_text(exc)}")
        return
    await state.clear()
    await message.answer("Комментарий покупателю сохранён в Playerok.")
    await render_item_card(message, account, client, item_id)


# --- УПРАВЛЕНИЕ ФОТОГРАФИЯМИ ---

@router.callback_query(F.data.startswith("item:edit:photo:"))
async def item_photo_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[3]
    await call.answer()

    client = await svc().gateway.get_client(account)
    try:
        item = await client.get_item(item_id)
        attachments = list(getattr(item, "attachments", []) or [])
    except Exception:
        attachments = []

    lines = [
        "🖼 <b>Управление фотографиями товара</b>\n",
        f"Текущих фото в лоте: <b>{len(attachments)} шт.</b>",
    ]
    for i, att in enumerate(attachments, start=1):
        url = getattr(att, "url", None)
        if url:
            lines.append(f"{i}. <a href=\"{url}\">Фото #{i} ↗</a>")

    lines.append("\nВыберите действие:")

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📸 Добавить фото (прикрепить)", callback_data=f"item:photo:add:{item_id}")],
            [InlineKeyboardButton(text="🔄 Заменить все фото на новое", callback_data=f"item:photo:replace:{item_id}")],
            [InlineKeyboardButton(text="🗑 Удалить все фото из лота", callback_data=f"item:photo:clear:{item_id}")],
            [InlineKeyboardButton(text="⬅️ Назад в карточку товара", callback_data=f"item:view:{item_id}")],
        ]
    )
    await edit(call, "\n".join(lines), markup, disable_web_page_preview=False)


@router.callback_query(F.data.startswith("item:photo:add:"))
async def item_photo_add_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[3]
    await state.clear()
    await state.set_state(ItemEdit.add_photo)
    await state.update_data(item_id=item_id, account_id=str(account.id))
    await call.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"item:edit:photo:{item_id}")]]
    )
    await edit(
        call,
        "📸 <b>Добавление фото к лоту</b>\n\n"
        "Отправьте фотографию (как фото или файл без сжатия) прямо в этот чат.\n\n"
        "Она будет добавлена к уже существующим фото товара.",
        markup,
    )


@router.callback_query(F.data.startswith("item:photo:replace:"))
async def item_photo_replace_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[3]
    await state.clear()
    await state.set_state(ItemEdit.replace_photo)
    await state.update_data(item_id=item_id, account_id=str(account.id))
    await call.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"item:edit:photo:{item_id}")]]
    )
    await edit(
        call,
        "🔄 <b>Полная замена фотографий лота</b>\n\n"
        "Отправьте новую фотографию (обложку) прямо в этот чат.\n\n"
        "⚠️ Все старые фотографии товара будут удалены, а новая станет главной обложкой.",
        markup,
    )


@router.message(ItemEdit.add_photo, F.photo)
@router.message(ItemEdit.replace_photo, F.photo)
async def item_save_photo(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    is_replace = (current_state == ItemEdit.replace_photo.state)

    data = await state.get_data()
    item_id = data.get("item_id")
    await state.clear()

    account = await active_account(message.from_user.id)
    if not account or not item_id:
        await message.answer("❌ Аккаунт не найден.")
        return

    wait_msg = await message.answer("⏳ Скачиваю фото и загружаю в Playerok…")
    try:
        # Скачиваем самое качественное превью из Telegram
        photo = message.photo[-1]
        buffer = io.BytesIO()
        await message.bot.download(photo.file_id, destination=buffer)
        image_bytes = buffer.getvalue()

        client = await svc().gateway.get_client(account)

        remove_attachments = None
        if is_replace:
            # При полной замене удаляем все старые ID вложений
            item = await client.get_item(item_id)
            old_atts = list(getattr(item, "attachments", []) or [])
            remove_attachments = [str(getattr(att, "id", "")) for att in old_atts if getattr(att, "id", None)]

        await client.update_item(
            item_id,
            add_attachments=[image_bytes],
            remove_attachments=remove_attachments,
        )
        await wait_msg.delete()
        action_text = "заменена на новую обложку" if is_replace else "успешно добавлена"
        await message.answer(f"✅ <b>Фотография товара {action_text}!</b>", parse_mode="HTML")
        await render_item_card(message, account, client, item_id)
    except Exception as exc:
        await wait_msg.delete()
        await message.answer(
            f"❌ <b>Ошибка при загрузке фото:</b>\n<code>{html.escape(str(exc))[:1500]}</code>",
            parse_mode="HTML",
            reply_markup=back_menu(f"item:edit:photo:{item_id}"),
        )


@router.callback_query(F.data.startswith("item:photo:clear:"))
async def item_photo_clear(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[3]
    await call.answer("Очищаю фото…")

    client = await svc().gateway.get_client(account)
    try:
        item = await client.get_item(item_id)
        old_atts = list(getattr(item, "attachments", []) or [])
        remove_attachments = [str(getattr(att, "id", "")) for att in old_atts if getattr(att, "id", None)]
        if not remove_attachments:
            await call.answer("У товара и так нет фотографий.", show_alert=True)
            return

        await client.update_item(item_id, remove_attachments=remove_attachments)
        await render_item_card(call, account, client, item_id)
    except Exception as exc:
        await edit(
            call,
            f"❌ <b>Ошибка при удалении фотографий:</b>\n<code>{html.escape(str(exc))[:1500]}</code>",
            back_menu(f"item:view:{item_id}"),
        )


# --- ПУБЛИКАЦИЯ ТОВАРА ---

@router.callback_query(F.data.startswith("item:publish:"))
async def item_publish_action(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.rsplit(":", 1)[1]
    data = await state.get_data()
    in_wizard = await state.get_state() == ItemCreate.priority.state and data.get("new_item_id") == item_id
    if in_wizard and data.get("account_id") != str(account.id):
        await call.answer("Аккаунт изменился. Откройте черновик из списка товаров.", show_alert=True)
        return
    if not in_wizard:
        await state.clear()
    await call.answer()
    await _show_publication(call, state, account, item_id, in_wizard)


def _publication_actions(item_id: str, back: str, slug: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Обновить статус лота", callback_data=f"item:view:{item_id}")
    if slug:
        builder.button(text="Открыть лот на Playerok", url=f"https://playerok.com/products/{slug}")
    if back != f"item:view:{item_id}":
        builder.button(text="Назад", callback_data=back)
    builder.adjust(1)
    return builder.as_markup()


def _listing_error_detail(exc: Exception) -> str:
    return f"\n<code>{html.escape(playerok_error_text(exc))}</code>"


def _publication_status_detail(item: Any) -> str:
    payment = getattr(item, "status_payment", None)
    messages = [
        strip_error_detail(text)
        for model in (item, payment)
        if isinstance((text := getattr(model, "status_description", None)), str) and text.strip()
    ]
    return "".join(f"\n<code>{html.escape(text)}</code>" for text in dict.fromkeys(messages))


async def _show_publication(
    call: CallbackQuery, state: FSMContext, account: Any, item_id: str,
    in_wizard: bool, notice: str = "",
) -> None:
    await state.update_data(publication=None)
    back = "itemdraft:review" if in_wizard else f"item:view:{item_id}"
    item = None
    step = "загрузить лот"
    try:
        client = await svc().gateway.get_client(account)
        item = await client.get_item(item_id)
        if item is None:
            await edit(call, "Playerok не вернул лот. Проверьте выбранный аккаунт и список черновиков.", back_menu("items:list:drafts"))
            return
        if not can_publish(item):
            await edit(
                call,
                f"Публикация сейчас недоступна. Статус на Playerok: {item_status_badge(item.status)}"
                + (_publication_status_detail(item) or "\nPlayerok не указал причину ограничения."),
                _publication_actions(item_id, back, getattr(item, "slug", None)),
            )
            return
        prices = int(item.price), tariff_price(item)
        step = "загрузить тарифы"
        tariffs = await publication_tariffs(client, item_id, prices[1])
        balance = await publication_balance(client)
    except Exception as exc:
        logger.warning("Playerok listing %s failed to load publication terms: %s", item_id, type(exc).__name__)
        await edit(
            call,
            f"Не удалось {step}.{_listing_error_detail(exc)}",
            _publication_actions(item_id, back, getattr(item, "slug", None)),
        )
        return

    token = secrets.token_hex(6)
    await state.update_data(publication={
        "token": token, "item_id": item_id, "account_id": str(account.id), "prices": prices,
        "tariffs": tariffs, "balance": balance, "in_wizard": in_wizard, "selected": None,
        "slug": getattr(item, "slug", None),
    })
    price, base = prices
    price_text = f"<s>{base} ₽</s> → <b>{price} ₽</b>" if price < base else f"<b>{price} ₽</b>"
    lines = [
        f"🚀 <b>Публикация · {html.escape(clip(item.name, 100))}</b>\n",
        f"Цена продажи: {price_text}",
        f"Доступный баланс: <b>{rubles(balance) if balance is not None else 'недоступен'}</b>",
    ]
    if notice:
        lines.append(f"\n{notice}")
    builder = InlineKeyboardBuilder()
    for idx, tariff in enumerate(tariffs):
        if not tariff.fee:
            builder.row(InlineKeyboardButton(text="Выставить бесплатно", callback_data=f"itempub:pick:{token}:{idx}"))
            continue
        if balance is None:
            details = "баланс недоступен"
        elif balance < tariff.fee:
            details = "недостаточно средств"
        else:
            details = f"остаток {rubles(balance - tariff.fee)}"
        label = f"{clip(tariff.name, 24)} · {rubles(tariff.fee)}"
        lines.append(f"{html.escape(label)} · {details}")
        if balance is not None and balance >= tariff.fee:
            builder.row(InlineKeyboardButton(text=label, callback_data=f"itempub:pick:{token}:{idx}"))
    if not tariffs:
        lines.append("Playerok пока не предложил тарифы для этого лота.")
    if in_wizard:
        builder.row(InlineKeyboardButton(text="Сохранить в черновиках", callback_data="itemprio:draft"))
    builder.row(InlineKeyboardButton(text="Обновить тарифы и баланс", callback_data=f"item:publish:{item_id}"))
    builder.row(InlineKeyboardButton(text="Назад", callback_data=back))
    await edit(call, "\n".join(lines), builder.as_markup())


@router.callback_query(F.data.startswith("itempub:pick:"))
async def item_publish_pick(call: CallbackQuery, state: FSMContext) -> None:
    _, _, token, idx = call.data.split(":")
    quote = (await state.get_data()).get("publication")
    if not quote or quote["token"] != token or not idx.isdigit() or int(idx) >= len(quote["tariffs"]):
        await call.answer("Условия устарели. Откройте экран публикации заново.", show_alert=True)
        return
    tariff = quote["tariffs"][int(idx)]
    balance = quote["balance"]
    if tariff.fee > 0 and (balance is None or balance < tariff.fee):
        await call.answer("Недостаточно средств или баланс недоступен.", show_alert=True)
        return
    confirm_token = secrets.token_hex(6)
    quote = {**quote, "selected": tariff, "confirm_token": confirm_token}
    await state.update_data(publication=quote)
    lines = [f"<b>Подтвердите публикацию</b>\n", f"Цена продажи: <b>{rubles(quote['prices'][0])}</b>"]
    if tariff.fee:
        lines += [
            f"Тариф: <b>{html.escape(tariff.name)}</b>",
            f"Будет списано: <b>{rubles(tariff.fee)}</b>",
            f"Остаток после списания: <b>{rubles(balance - tariff.fee)}</b>",
        ]
    else:
        lines.append("Стоимость размещения: <b>0 ₽</b>")
    await call.answer()
    await edit(call, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Подтвердить и списать {rubles(tariff.fee)}" if tariff.fee else "Подтвердить бесплатное размещение",
            callback_data=f"itempub:confirm:{confirm_token}",
        )],
        [InlineKeyboardButton(text="Назад к тарифам", callback_data=f"item:publish:{quote['item_id']}")],
    ]))


@router.callback_query(F.data.startswith("itempub:confirm:"))
async def item_publish_confirm(call: CallbackQuery, state: FSMContext) -> None:
    quote = (await state.get_data()).get("publication")
    token = call.data.rsplit(":", 1)[1]
    if not quote or quote.get("confirm_token") != token or not quote["selected"]:
        await call.answer("Условия устарели. Откройте экран публикации заново.", show_alert=True)
        return
    account = await require_account(call)
    if not account:
        return
    if str(account.id) != quote["account_id"]:
        await call.answer("Аккаунт изменился. Откройте товар заново.", show_alert=True)
        return
    await call.answer("Проверяю условия публикации…")
    await state.update_data(publication=None)
    item_id, tariff = quote["item_id"], quote["selected"]
    actions = _publication_actions(item_id, f"item:view:{item_id}", quote.get("slug"))
    try:
        client = await svc().gateway.get_client(account)
        item = await confirm_publication(client, item_id, quote["prices"], tariff, quote["balance"])
    except PublicationChanged:
        await _show_publication(
            call, state, account, item_id, quote["in_wizard"], "Условия изменились. Выберите тариф заново.",
        )
        return
    except PublicationUnconfirmed as exc:
        current_status = item_status_badge(getattr(exc.item, "status", None))
        detail = _publication_status_detail(exc.item)
        if exc.failure:
            detail += _listing_error_detail(exc.failure)
        if not detail:
            detail = "\nPlayerok вернул этот статус без пояснения. Проверьте карточку лота на сайте."
        await edit(call, f"Playerok пока не подтвердил публикацию. Статус: {current_status}{detail}", actions)
        return
    except Exception as exc:
        logger.warning("Playerok listing %s publication failed: %s", item_id, type(exc).__name__)
        await edit(call, f"Не удалось опубликовать лот.{_listing_error_detail(exc)}", actions)
        return

    await state.clear()
    status = getattr(item, "status", None)
    lines = [
        f"<b>{html.escape(clip(getattr(item, 'name', None) or 'Товар', 100))}</b>",
        f"Статус на Playerok: {item_status_badge(status)}",
    ]
    if tariff.fee:
        balance = await publication_balance(client)
        if balance is not None:
            lines.append(f"Текущий баланс: <b>{rubles(balance)}</b>")
    await edit(call, "\n".join(lines), back_menu(f"item:view:{item_id}"))


@router.callback_query(F.data.startswith("item:delete:ask:"))
async def item_delete_ask(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[3]
    await call.answer()

    client = await svc().gateway.get_client(account)
    try:
        item = await client.get_item(item_id)
        name = getattr(item, "name", "Товар")
    except Exception:
        name = "Товар"

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить навсегда", callback_data=f"item:delete:confirm:{item_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"item:view:{item_id}")],
        ]
    )
    await edit(
        call,
        f"❓ <b>Вы уверены, что хотите удалить товар?</b>\n\n"
        f"• <b>{html.escape(str(name))}</b>\n"
        f"• ID: <code>{html.escape(item_id)}</code>\n\n"
        "⚠️ Товар будет безвозвратно удален с вашего аккаунта Playerok.",
        markup,
    )


@router.callback_query(F.data.startswith("item:delete:confirm:"))
async def item_delete_confirm(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[3]
    await call.answer("Удаляю…")

    client = await svc().gateway.get_client(account)
    try:
        await client.remove_item(item_id)
        await edit(
            call,
            f"🗑 <b>Товар <code>{html.escape(item_id)}</code> успешно удален!</b>",
            back_menu("items:list"),
        )
    except Exception as exc:
        await edit(
            call,
            f"❌ <b>Ошибка при удалении товара:</b>\n<code>{html.escape(str(exc))[:1500]}</code>",
            back_menu(f"item:view:{item_id}"),
        )
