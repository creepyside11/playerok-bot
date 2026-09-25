from __future__ import annotations

import html
import io
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .handlers import active_account, clip, edit, require_account, svc
from .keyboards import back_menu
from .states import ItemEdit

logger = logging.getLogger("items_manager")
router = Router(name="items_manager")

PAGE_SIZE = 8


def item_status_badge(status: Any) -> str:
    raw = str(getattr(status, "name", status) or "").upper()
    if raw in ("APPROVED", "ACTIVE"):
        return "🟢 На продаже"
    elif raw in ("PENDING", "MODERATION", "ON_MODERATION"):
        return "⏳ На модерации"
    elif raw in ("SOLD", "COMPLETED"):
        return "📦 Продан"
    elif raw in ("PAUSED", "DISABLED", "ARCHIVED"):
        return "⏸ Снят с продажи"
    return f"ℹ️ {raw}"


@router.callback_query(F.data == "items:list")
@router.callback_query(F.data.startswith("items:list:"))
async def items_list_view(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return

    page_idx = 0
    parts = (call.data or "").split(":")
    if len(parts) >= 3 and parts[2].isdigit():
        page_idx = int(parts[2])

    await call.answer("Загружаю товары…")
    try:
        client = await svc().gateway.get_client(account)
        # Загружаем товары продавца без фильтрации по статусу
        res = await client.call("get_my_items", statuses=None, count=60)
        all_items = list(getattr(res, "items", []) or [])
    except Exception as exc:
        await edit(
            call,
            f"❌ <b>Ошибка при загрузке списка товаров:</b>\n\n<code>{html.escape(str(exc))[:1500]}</code>",
            back_menu("items"),
        )
        return

    if not all_items:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Выставить товар (Поиск)", callback_data="catalog:search")],
                [InlineKeyboardButton(text="⬅️ Меню товаров", callback_data="menu:items")],
            ]
        )
        await edit(call, "📦 <b>У вас пока нет товаров на Playerok.</b>\n\nВы можете выставить первый товар кнопкой ниже:", markup)
        return

    total_count = len(all_items)
    start_idx = page_idx * PAGE_SIZE
    current_page_items = all_items[start_idx : start_idx + PAGE_SIZE]
    total_pages = (total_count + PAGE_SIZE - 1) // PAGE_SIZE

    builder = InlineKeyboardBuilder()

    for item in current_page_items:
        i_id = str(getattr(item, "id", ""))
        name = str(getattr(item, "name", "Товар") or "Товар")
        price = getattr(item, "price", 0)
        status_raw = str(getattr(getattr(item, "status", None), "name", "")).upper()
        icon = "🟢" if status_raw in ("APPROVED", "ACTIVE") else ("⏳" if status_raw in ("PENDING", "MODERATION") else "⏸")
        btn_text = f"{icon} {price} ₽ · {clip(name, 35)}"
        builder.button(text=btn_text, callback_data=f"item:view:{i_id}")

    builder.adjust(1)

    # Пагинация
    nav_buttons = []
    if page_idx > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"items:list:{page_idx - 1}"))
    if page_idx + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"items:list:{page_idx + 1}"))
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        InlineKeyboardButton(text="➕ Выставить товар", callback_data="catalog:search"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"items:list:{page_idx}"),
    )
    builder.row(InlineKeyboardButton(text="⬅️ Меню товаров", callback_data="menu:items"))

    text = (
        f"📋 <b>Ваши товары ({account.username})</b>\n\n"
        f"Всего лотов: <b>{total_count}</b> (страница {page_idx + 1} из {max(1, total_pages)})\n\n"
        "💡 <i>Нажмите на любой товар ниже, чтобы изменить цену, название, описание или фото:</i>"
    )
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
    prev_price = getattr(item, "prev_price", None)
    desc = str(getattr(item, "description", "") or "").strip()
    status = getattr(item, "status", None)
    status_label = item_status_badge(status)
    attachments = list(getattr(item, "attachments", []) or [])
    game = getattr(item, "game", None)
    game_name = str(getattr(game, "name", "—") or "—")
    category = getattr(item, "category", None)
    category_name = str(getattr(category, "name", "—") or "—")

    price_str = f"<b>{price} ₽</b>"
    if prev_price and int(prev_price) != int(price):
        price_str += f" <i>(ранее: {prev_price} ₽)</i>"

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
        InlineKeyboardButton(text="📝 Изменить описание", callback_data=f"item:edit:desc:{item_id}"),
        InlineKeyboardButton(text=f"🖼 Фото ({photos_count})", callback_data=f"item:edit:photo:{item_id}"),
    )

    status_name = str(getattr(status, "name", "")).upper()
    if status_name not in ("APPROVED", "ACTIVE"):
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
async def item_publish_action(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    item_id = call.data.split(":")[2]
    await call.answer("Публикую товар…")

    client = await svc().gateway.get_client(account)
    try:
        item = await client.get_item(item_id)
        price = getattr(item, "price", 0)
        statuses = await client.get_item_priority_statuses(item_id, price)
        if not statuses:
            raise RuntimeError("Не удалось получить статусы приоритета для публикации")

        # Берем базовый/бесплатный приоритет
        priority_id = str(statuses[0].id)
        await client.publish_item(item_id, priority_status_id=priority_id)
        await call.answer("✅ Товар успешно выставлен на продажу!", show_alert=True)
        await render_item_card(call, account, client, item_id)
    except Exception as exc:
        await edit(
            call,
            f"❌ <b>Не удалось опубликовать товар:</b>\n<code>{html.escape(str(exc))[:1500]}</code>",
            back_menu(f"item:view:{item_id}"),
        )


# --- УДАЛЕНИЕ ТОВАРА ---

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
