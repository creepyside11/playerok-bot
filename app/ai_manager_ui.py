"""
Пользовательский интерфейс и обработчики AI-Менеджера для управления настройками ИИ
и выполнения автономных задач через LLM.
"""

import copy
import html
import json
import logging
from typing import Any
from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from .ai_manager import UniversalImageClient, UniversalOpenAIClient
from .handlers import active_account, clip, edit, require_account, svc
from .keyboards import back_menu, main_menu
from .models import AutoReplyRule, DeliveryRule, DeliveryStock, PlayerokAccount
from .plugin_system import PluginManager, PluginState
from .states import AIManagerState

logger = logging.getLogger("ai_manager_ui")
router = Router(name="ai_manager_ui")

_plugin_mgr: PluginManager | None = None


def set_plugin_manager(pm: PluginManager) -> None:
    global _plugin_mgr
    _plugin_mgr = pm


def get_ai_settings(account: PlayerokAccount) -> dict[str, Any]:
    cfg = dict(account.settings or {})
    return cfg.get("ai_manager", {
        "llm_api_key": "",
        "llm_base_url": "https://api.openai.com/v1",
        "llm_model": "gpt-4o-mini",
        "image_api_key": "",
        "image_base_url": "https://api.openai.com/v1",
        "image_model": "gpt-2.5-image",
    })


def get_llm_client(account: PlayerokAccount) -> UniversalOpenAIClient:
    s = get_ai_settings(account)
    return UniversalOpenAIClient(
        api_key=s.get("llm_api_key", ""),
        base_url=s.get("llm_base_url", "https://api.openai.com/v1"),
        model=s.get("llm_model", "gpt-4o-mini"),
    )


def get_image_client(account: PlayerokAccount) -> UniversalImageClient:
    s = get_ai_settings(account)
    key = s.get("image_api_key") or s.get("llm_api_key") or ""
    return UniversalImageClient(
        api_key=key,
        base_url=s.get("image_base_url", "https://api.openai.com/v1"),
        model=s.get("image_model", "gpt-2.5-image"),
    )


@router.callback_query(F.data == "menu:ai_manager")
async def ai_manager_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    cfg = get_ai_settings(account)

    llm_key_set = "🟢 Задан" if cfg.get("llm_api_key") else "🔴 Не задан"
    img_key_set = "🟢 Задан" if (cfg.get("image_api_key") or cfg.get("llm_api_key")) else "⚪ Не задан"

    text = (
        "🧠 <b>Центр управления ИИ (AI Agent)</b>\n\n"
        "Здесь вы можете настроить нейросети и давать ИИ любые команды:\n"
        "• <b>Выставлять товары</b> с автогенерацией описания, цены и обложек\n"
        "• <b>Привязывать автовыдачу</b> лотов к плагинам (Black Russia, Roblox, Supercell и др.)\n"
        "• <b>Настраивать бота</b> под ваши задачи в автоматическом режиме\n\n"
        f"💬 <b>Текстовая LLM:</b> {html.escape(cfg.get('llm_model', 'gpt-4o-mini'))} ({llm_key_set})\n"
        f"🌐 <i>URL:</i> <code>{html.escape(cfg.get('llm_base_url', 'https://api.openai.com/v1'))}</code>\n\n"
        f"🎨 <b>Image AI (Фото/Обложки):</b> {html.escape(cfg.get('image_model', 'gpt-2.5-image'))} ({img_key_set})\n"
        f"🌐 <i>URL:</i> <code>{html.escape(cfg.get('image_base_url', 'https://api.openai.com/v1'))}</code>\n"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Дать задачу ИИ (текст или фото)", callback_data="ai:agent:task")],
        [InlineKeyboardButton(text="🤖 Умная автовыдача ИИ: " + ("ВКЛ" if cfg.get("auto_delivery_enabled") else "ВЫКЛ"), callback_data="ai:toggle:autodelivery")],
        [InlineKeyboardButton(text="📝 Инструкция для автовыдачи ИИ", callback_data="ai:set:autodeliv_inst")],
        [InlineKeyboardButton(text="⚙️ Настройка LLM модели", callback_data="ai:settings:llm")],
        [InlineKeyboardButton(text="🎨 Настройка Image AI (Фото)", callback_data="ai:settings:image")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main")],
    ])
    await edit(call, text, markup)


@router.callback_query(F.data == "ai:toggle:autodelivery")
async def ai_toggle_autodelivery(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        s.setdefault("ai_manager", {})
        cur = bool(s["ai_manager"].get("auto_delivery_enabled", False))
        s["ai_manager"]["auto_delivery_enabled"] = not cur
        row.settings = copy.deepcopy(s)
        await session.commit()
        account.settings = s
    await call.answer("Умная автовыдача переключена")
    await ai_manager_menu(call, None)  # type: ignore


@router.callback_query(F.data == "ai:set:autodeliv_inst")
async def ai_set_autodeliv_inst_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    cfg = get_ai_settings(account)
    cur_inst = cfg.get("auto_delivery_instruction") or "Сгенерируй вежливое сообщение выдачи товара с инструкцией для покупателя."
    await state.set_state(AIManagerState.auto_delivery_instruction)
    await call.answer()
    await edit(
        call,
        "📝 <b>Инструкция для автономной выдачи заказов через ИИ</b>\n\n"
        f"Текущая инструкция:\n<code>{html.escape(cur_inst)}</code>\n\n"
        "Отправьте текст инструкции (как ИИ должен приветствовать покупателя, откуда брать данные или как инструктировать):\n\n"
        "Для отмены: /cancel",
        back_menu("ai_manager"),
    )


@router.message(AIManagerState.auto_delivery_instruction, F.text)
async def ai_save_autodeliv_inst(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    inst = (message.text or "").strip()
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        s.setdefault("ai_manager", {})
        s["ai_manager"]["auto_delivery_instruction"] = inst
        row.settings = copy.deepcopy(s)
        await session.commit()
    await state.clear()
    await message.answer("✅ Инструкция автовыдачи ИИ сохранена!", reply_markup=main_menu())


# ==================== НАСТРОЙКИ LLM ====================

@router.callback_query(F.data == "ai:settings:llm")
async def ai_settings_llm(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    cfg = get_ai_settings(account)
    key_mask = (cfg.get("llm_api_key")[:6] + "..." + cfg.get("llm_api_key")[-4:]) if len(cfg.get("llm_api_key", "")) > 10 else ("задан" if cfg.get("llm_api_key") else "не задан")

    text = (
        "💬 <b>Настройка текстовой LLM (OpenAI-совместимый API)</b>\n\n"
        f"• <b>API Key:</b> <code>{html.escape(key_mask)}</code>\n"
        f"• <b>Модель:</b> <code>{html.escape(cfg.get('llm_model', 'gpt-4o-mini'))}</code>\n"
        f"• <b>API Base URL:</b> <code>{html.escape(cfg.get('llm_base_url', 'https://api.openai.com/v1'))}</code>\n\n"
        "<i>Подходят ключи OpenAI, OpenRouter, Groq, DeepSeek, vLLM и любых совместимых провайдеров.</i>"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Изменить API Key", callback_data="ai:set:llm_key")],
        [InlineKeyboardButton(text="🧠 Изменить модель", callback_data="ai:set:llm_model")],
        [InlineKeyboardButton(text="🌐 Изменить API URL", callback_data="ai:set:llm_url")],
        [InlineKeyboardButton(text="⬅️ Назад в Управление ИИ", callback_data="menu:ai_manager")],
    ])
    await edit(call, text, markup)


@router.callback_query(F.data.startswith("ai:set:llm_"))
async def ai_set_llm_param(call: CallbackQuery, state: FSMContext) -> None:
    param = call.data.split(":", 2)[2]
    await call.answer()
    if param == "llm_key":
        await state.set_state(AIManagerState.llm_key)
        await edit(call, "🔑 <b>Введите API-ключ для LLM</b>:\n\n<i>Сообщение будет удалено для безопасности. Для отмены: /cancel</i>", back_menu("ai_manager"))
    elif param == "llm_model":
        await state.set_state(AIManagerState.llm_model)
        await edit(call, "🧠 <b>Введите название модели</b> (например <code>gpt-4o-mini</code>, <code>gpt-4o</code>, <code>deepseek-chat</code>):\n\nДля отмены: /cancel", back_menu("ai_manager"))
    elif param == "llm_url":
        await state.set_state(AIManagerState.llm_base_url)
        await edit(call, "🌐 <b>Введите Base URL API</b> (например <code>https://api.openai.com/v1</code> или <code>https://openrouter.ai/api/v1</code>):\n\nДля отмены: /cancel", back_menu("ai_manager"))


@router.message(AIManagerState.llm_key, F.text)
async def ai_save_llm_key(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        s.setdefault("ai_manager", {})
        s["ai_manager"]["llm_api_key"] = token
        row.settings = copy.deepcopy(s)
        await session.commit()
    await state.clear()
    await message.answer("✅ API-ключ LLM сохранён!", reply_markup=main_menu())


@router.message(AIManagerState.llm_model, F.text)
async def ai_save_llm_model(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    model = (message.text or "").strip()
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        s.setdefault("ai_manager", {})
        s["ai_manager"]["llm_model"] = model
        row.settings = copy.deepcopy(s)
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Модель LLM установлена: <code>{html.escape(model)}</code>", parse_mode="HTML", reply_markup=main_menu())


@router.message(AIManagerState.llm_base_url, F.text)
async def ai_save_llm_url(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    url = (message.text or "").strip()
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        s.setdefault("ai_manager", {})
        s["ai_manager"]["llm_base_url"] = url
        row.settings = copy.deepcopy(s)
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Base URL LLM установлен: <code>{html.escape(url)}</code>", parse_mode="HTML", reply_markup=main_menu())


# ==================== НАСТРОЙКИ IMAGE AI ====================

@router.callback_query(F.data == "ai:settings:image")
async def ai_settings_image(call: CallbackQuery) -> None:
    account = await require_account(call)
    if not account:
        return
    await call.answer()
    cfg = get_ai_settings(account)
    key_mask = (cfg.get("image_api_key")[:6] + "..." + cfg.get("image_api_key")[-4:]) if len(cfg.get("image_api_key", "")) > 10 else ("задан" if cfg.get("image_api_key") else "не задан (используется ключ LLM)")

    text = (
        "🎨 <b>Настройка Image AI для фото и обложек</b>\n\n"
        f"• <b>API Key:</b> <code>{html.escape(key_mask)}</code>\n"
        f"• <b>Модель генерации:</b> <code>{html.escape(cfg.get('image_model', 'gpt-2.5-image'))}</code>\n"
        f"• <b>API Base URL:</b> <code>{html.escape(cfg.get('image_base_url', 'https://api.openai.com/v1'))}</code>\n\n"
        "<i>Используется для автогенерации продающих карточек и фото товаров через DALL-E / Midjourney-API / OpenAI Images.</i>"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Изменить Image API Key", callback_data="ai:set:img_key")],
        [InlineKeyboardButton(text="🎨 Изменить модель (dall-e-3/gpt-2.5-image)", callback_data="ai:set:img_model")],
        [InlineKeyboardButton(text="🌐 Изменить Image Base URL", callback_data="ai:set:img_url")],
        [InlineKeyboardButton(text="⬅️ Назад в Управление ИИ", callback_data="menu:ai_manager")],
    ])
    await edit(call, text, markup)


@router.callback_query(F.data.startswith("ai:set:img_"))
async def ai_set_image_param(call: CallbackQuery, state: FSMContext) -> None:
    param = call.data.split(":", 2)[2]
    await call.answer()
    if param == "img_key":
        await state.set_state(AIManagerState.image_key)
        await edit(call, "🔑 <b>Введите API-ключ для генерации фото</b> (или отправьте «-», чтобы использовать ключ LLM):\n\nДля отмены: /cancel", back_menu("ai_manager"))
    elif param == "img_model":
        await state.set_state(AIManagerState.image_model)
        await edit(call, "🎨 <b>Введите название модели изображений</b> (например <code>gpt-2.5-image</code> или <code>dall-e-3</code>):\n\nДля отмены: /cancel", back_menu("ai_manager"))
    elif param == "img_url":
        await state.set_state(AIManagerState.image_base_url)
        await edit(call, "🌐 <b>Введите Base URL для генерации фото</b> (по умолчанию <code>https://api.openai.com/v1</code>):\n\nДля отмены: /cancel", back_menu("ai_manager"))


@router.message(AIManagerState.image_key, F.text)
async def ai_save_image_key(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if token in {"-", "—", "clear"}:
        token = ""
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        s.setdefault("ai_manager", {})
        s["ai_manager"]["image_api_key"] = token
        row.settings = copy.deepcopy(s)
        await session.commit()
    await state.clear()
    await message.answer("✅ API-ключ для фото обновлён!", reply_markup=main_menu())


@router.message(AIManagerState.image_model, F.text)
async def ai_save_image_model(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    model = (message.text or "").strip()
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        s.setdefault("ai_manager", {})
        s["ai_manager"]["image_model"] = model
        row.settings = copy.deepcopy(s)
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Модель фото установлена: <code>{html.escape(model)}</code>", parse_mode="HTML", reply_markup=main_menu())


@router.message(AIManagerState.image_base_url, F.text)
async def ai_save_image_url(message: Message, state: FSMContext) -> None:
    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return
    url = (message.text or "").strip()
    async with svc().db() as session:
        row = await session.get(PlayerokAccount, account.id)
        s = dict(row.settings or {})
        s.setdefault("ai_manager", {})
        s["ai_manager"]["image_base_url"] = url
        row.settings = copy.deepcopy(s)
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Base URL для фото установлен: <code>{html.escape(url)}</code>", parse_mode="HTML", reply_markup=main_menu())


# ==================== АВТОНОМНЫЙ ИИ АГЕНТ ====================

@router.callback_query(F.data == "ai:agent:task")
async def ai_agent_task_prompt(call: CallbackQuery, state: FSMContext) -> None:
    account = await require_account(call)
    if not account:
        return
    cfg = get_ai_settings(account)
    if not cfg.get("llm_api_key"):
        await call.answer("⚠️ Сначала настройте API-ключ в настройках LLM!", show_alert=True)
        return

    await call.answer()
    await state.set_state(AIManagerState.task)
    text = (
        "🤖 <b>Дайте задачу ИИ-агенту (с текстом или фото)</b>\n\n"
        "Отправьте текст или <b>фотографию с описанием</b> (скриншот аккаунта, лот конкурента, страница API).\n\n"
        "💡 <b>ИИ умеет:</b>\n"
        "• <b>Анализировать конкурентов и сайты</b> через веб-доступ (укажите ссылку или запрос цен)\n"
        "• <b>Работать с внешними API</b> (делать запросы, забирать ключи/данные и выдавать покупателям)\n"
        "• <b>Анализировать фото</b> (распознавать параметры аккаунта со скрина и выставлять лот)\n"
        "• <b>Выставлять товары</b> с генерацией продающей обложки и описания\n"
        "• <b>Настраивать бота</b> и привязывать автовыдачу без лишних плагинов\n\n"
        "Для отмены отправьте /cancel."
    )
    await edit(call, text, back_menu("ai_manager"))


SYSTEM_AGENT_PROMPT = """Ты — интеллектуальный автономный агент управления ботом Playerok с доступом к веб-инструментам, анализу фото и API маркетплейса.
Твоя цель — решить задачу пользователя наилучшим образом.

Ты можешь выполнять действия:
1. create_item: создание и выставление товара на Playerok
   - name: привлекательное продающее название лота
   - description: продающее структурированное описание
   - price: цена в рублях (целое число)
   - discount_price: (опционально) цена со скидкой, меньше price
   - game_name: название игры (например "Brawl Stars", "Roblox", "Black Russia", "Telegram", "Gmail", "Steam")
   - generate_cover: true / false (сгенерировать ли продающую AI-обложку)
   - cover_prompt: промпт для генерации картинки на английском (3D game render)
   - delivery_text: данные для выдачи покупателю (логин, пароль, ключ, код)
   - delivery_comment: инструкция или правила гарантии для покупателя
   - bind_plugin: id плагина для привязки если применимо

2. web_fetch: запросить данные из интернета, цены конкурентов или вызвать внешний REST API
   - url: URL для запроса
   - method: "GET" или "POST"
   - headers: (опционально) заголовки
   - body: (опционально) тело запроса для POST

3. configure_bot: настройка параметров бота
   - auto_confirm: true / false
   - auto_delivery_enabled: true / false
   - auto_delivery_instruction: инструкция для выдачи
   - notifications: словарь с ключами new_message, new_deal, deal_status (true/false)

4. configure_plugin: включение или настройка плагина
   - plugin_id: id плагина
   - enabled: true / false
   - config: словарь настроек

5. general_response: развёрнутый ответ пользователю

Ответ должен быть СТРОГО в формате JSON без markdown-обёртки:
{
  "thought": "Краткое объяснение действий на русском",
  "action": "create_item" | "web_fetch" | "configure_bot" | "configure_plugin" | "general_response",
  "data": { ...соответствующие поля... },
  "message": "Понятное сообщение пользователю о выполненных действиях"
}
"""


@router.message(AIManagerState.task, F.photo)
async def ai_agent_task_photo_execute(message: Message, state: FSMContext) -> None:
    caption = (message.caption or "").strip() or "Проанализируй фото товара, определи игру, параметры и создай лот с автовыдачей."
    photo = message.photo[-1]

    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return

    wait_msg = await message.answer("🤖 <b>ИИ загружает фото и анализирует задачу…</b>", parse_mode="HTML")
    await state.clear()

    # Скачиваем фото в base64 для передачи в LLM Vision API
    bot = message.bot
    f = await bot.get_file(photo.file_id)
    buf = await bot.download_file(f.file_path)
    b64_image = base64.b64encode(buf.getvalue()).decode("utf-8")
    data_uri = f"data:image/jpeg;base64,{b64_image}"

    llm = get_llm_client(account)
    img_client = get_image_client(account)

    messages = [
        {"role": "system", "content": SYSTEM_AGENT_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": caption},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        },
    ]

    await _process_ai_agent_pipeline(message, wait_msg, account, llm, img_client, messages)


@router.message(AIManagerState.task, F.text)
async def ai_agent_task_execute(message: Message, state: FSMContext) -> None:
    user_task = (message.text or "").strip()
    if not user_task:
        return

    account = await active_account(message.from_user.id)
    if not account:
        await state.clear()
        return

    wait_msg = await message.answer("🤖 <b>ИИ анализирует задачу и выполняет действия…</b>", parse_mode="HTML")
    await state.clear()

    llm = get_llm_client(account)
    img_client = get_image_client(account)

    messages = [
        {"role": "system", "content": SYSTEM_AGENT_PROMPT},
        {"role": "user", "content": user_task},
    ]

    await _process_ai_agent_pipeline(message, wait_msg, account, llm, img_client, messages)


async def _process_ai_agent_pipeline(
    message: Message,
    wait_msg: Message,
    account: PlayerokAccount,
    llm: UniversalOpenAIClient,
    img_client: UniversalImageClient,
    messages: list[dict[str, Any]],
) -> None:
    try:
        raw_response = await llm.chat_completion(
            messages=messages,
            temperature=0.3,
            response_json=True,
        )
        plan = json.loads(raw_response)
    except Exception as exc:
        await wait_msg.delete()
        await message.answer(f"❌ <b>Ошибка ИИ:</b> {html.escape(str(exc))}", parse_mode="HTML", reply_markup=main_menu())
        return

    action = plan.get("action")
    data = plan.get("data") or {}

    # Если ИИ решил сделать веб-запрос для изучения цен конкурентов или обращения к API
    if action == "web_fetch" and data.get("url"):
        target_url = str(data["url"])
        await wait_msg.edit_text(f"🌐 <b>ИИ делает веб-запрос к:</b> <code>{html.escape(target_url[:60])}</code>…", parse_mode="HTML")
        from .ai_manager import web_request
        web_res = await web_request(
            target_url,
            method=data.get("method", "GET"),
            headers=data.get("headers"),
            data=data.get("body"),
        )
        content_preview = web_res.get("content", "")[:6000]

        # Передаем результат веб-запроса обратно в ИИ для окончательного решения
        messages.append({"role": "assistant", "content": json.dumps(plan)})
        messages.append({
            "role": "user",
            "content": f"Результат веб-запроса (HTTP {web_res.get('status')}):\n{content_preview}\n\nТеперь заверши задачу и верни итоговое действие (например create_item или general_response).",
        })

        try:
            raw_response2 = await llm.chat_completion(
                messages=messages,
                temperature=0.3,
                response_json=True,
            )
            plan = json.loads(raw_response2)
            action = plan.get("action")
            data = plan.get("data") or {}
        except Exception as exc2:
            await wait_msg.delete()
            await message.answer(f"❌ <b>Ошибка повторной обработки:</b> {html.escape(str(exc2))}", parse_mode="HTML", reply_markup=main_menu())
            return

    report = []
    report.append(f"💡 <b>ИИ:</b> {html.escape(plan.get('thought', 'Задача обработана'))}\n")

    # 1. Выставление товара через ИИ
    if action == "create_item":
        client = await svc().gateway.get_client(account)
        item_name = data.get("name") or "Товар от ИИ"
        price = int(data.get("price") or 500)
        desc = data.get("description") or "Качественный товар с быстрой выдачей."
        disc_price = data.get("discount_price")
        game_query = data.get("game_name") or "Другое"

        # Ищем подходящую игру и категорию через API
        target_cat_id = None
        target_obt_id = None
        try:
            games_page = await client.call("get_games", count=24, search=game_query)
            games_list = list(getattr(games_page, "games", []) or [])
            if games_list:
                g_id = games_list[0].id
                g_profile = await client.call("get_game", id=g_id)
                cats = list(getattr(g_profile, "categories", []) or [])
                if cats:
                    target_cat_id = str(cats[0].id)
                    obts_page = await client.call("get_game_category_obtaining_types", target_cat_id, count=10)
                    obts = list(getattr(obts_page, "obtaining_types", []) or [])
                    if obts:
                        target_obt_id = str(obts[0].id)
        except Exception as search_err:
            logger.warning("Auto category search error: %s", search_err)

        if not target_cat_id:
            # Fallback на первый попавшийся раздел
            try:
                games_page = await client.call("get_games", count=5)
                g0 = list(getattr(games_page, "games", []) or [])[0]
                gp = await client.call("get_game", id=g0.id)
                target_cat_id = str(list(getattr(gp, "categories", []) or [])[0].id)
            except Exception:
                target_cat_id = "1"

        # Генерация обложки если запрошена
        attachments = []
        if data.get("generate_cover") and img_client.api_key:
            cover_prompt = data.get("cover_prompt") or f"3D gaming marketplace banner for {item_name}, 4k render, epic lighting"
            await wait_msg.edit_text("🎨 <b>ИИ генерирует продающую обложку…</b>", parse_mode="HTML")
            cover_bytes = await img_client.generate_image(cover_prompt)
            if cover_bytes:
                attachments.append(cover_bytes)
                report.append("🖼 <i>Сгенерирована продающая AI-обложка лота</i>")

        await wait_msg.edit_text("⏳ <b>Публикую лот на Playerok…</b>", parse_mode="HTML")
        try:
            created_item = await client.create_item(
                game_category_id=target_cat_id,
                obtaining_type_id=target_obt_id,
                name=item_name,
                price=price,
                description=desc,
                options={},
                data_fields=[],
                attachments=attachments,
            )

            # Применяем скидку если была запрошена
            if disc_price and int(disc_price) < price:
                try:
                    await client.call("update_item", str(created_item.id), price=int(disc_price))
                    report.append(f"🔥 <i>Установлена скидка: {disc_price} ₽ (было {price} ₽)</i>")
                except Exception:
                    pass

            # Публикуем
            try:
                statuses = await client.get_item_priority_statuses(str(created_item.id), price)
                prio_id = str(statuses[0].id) if statuses else "1"
                await client.publish_item(str(created_item.id), prio_id)
            except Exception:
                pass

            report.append(f"✅ <b>Лот успешно опубликован!</b>")
            report.append(f"• Товар: <b>{html.escape(item_name)}</b>")
            report.append(f"• ID: <code>{created_item.id}</code>")
            report.append(f"• Цена: <b>{price} ₽</b>")

            # Автовыдача и комментарий
            deliv_text = data.get("delivery_text")
            deliv_comment = data.get("delivery_comment")
            if deliv_text:
                full_deliv = f"{deliv_text}\n\n📝 Инструкция / Комментарий:\n{deliv_comment}" if deliv_comment else deliv_text
                async with svc().db() as session:
                    rule = DeliveryRule(
                        account_id=account.id,
                        item_id=str(created_item.id),
                        mode="static",
                        message_template=full_deliv,
                        enabled=True,
                    )
                    session.add(rule)
                    await session.flush()
                    session.add(DeliveryStock(
                        rule_id=rule.id,
                        payload_encrypted=svc().cipher.encrypt(full_deliv)
                    ))
                    await session.commit()
                report.append("⚡ <i>Автовыдача и комментарий покупателю успешно привязаны к лоту!</i>")

        except Exception as create_exc:
            report.append(f"❌ Ошибка создания лота: {html.escape(str(create_exc))}")

    # 2. Настройка бота
    elif action == "configure_bot":
        async with svc().db() as session:
            row = await session.get(PlayerokAccount, account.id)
            s = dict(row.settings or {})
            if "auto_confirm" in data:
                s["auto_confirm"] = bool(data["auto_confirm"])
                report.append(f"• Автоподтверждение: <b>{'Включено' if s['auto_confirm'] else 'Выключено'}</b>")
            if "notifications" in data and isinstance(data["notifications"], dict):
                s.setdefault("notifications", {})
                s["notifications"].update(data["notifications"])
                report.append("• Настройки уведомлений обновлены")
            row.settings = copy.deepcopy(s)
            await session.commit()
        report.append("✅ <b>Параметры бота успешно применены!</b>")

    # 3. Настройка плагина
    elif action == "configure_plugin":
        plugin_id = data.get("plugin_id")
        enabled = bool(data.get("enabled", True))
        cfg_patch = data.get("config") or {}
        if plugin_id:
            async with svc().db() as session:
                st = await session.scalar(
                    select(PluginState).where(PluginState.account_id == account.id, PluginState.plugin_id == plugin_id)
                )
                if not st:
                    st = PluginState(account_id=account.id, plugin_id=plugin_id, enabled=enabled, config=cfg_patch)
                    session.add(st)
                else:
                    st.enabled = enabled
                    c = dict(st.config or {})
                    c.update(cfg_patch)
                    st.config = c
                await session.commit()
            report.append(f"🧩 Плагин <b>{html.escape(plugin_id)}</b>: <b>{'Включен' if enabled else 'Выключен'}</b>")
            report.append("✅ <b>Настройки плагина сохранены!</b>")

    # Обычный ответ
    else:
        report.append(html.escape(plan.get("message", "Задача выполнена.")))

    await wait_msg.delete()
    final_text = "\n".join(report)
    await message.answer(final_text, parse_mode="HTML", reply_markup=main_menu())
