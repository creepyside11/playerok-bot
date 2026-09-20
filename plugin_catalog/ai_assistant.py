from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

PLUGIN_META = {
    "id": "ai_assistant",
    "name": "ИИ Ассистент (#помощь)",
    "version": "1.0.0",
    "author": "Playerok Bot",
    "description": (
        "Интеллектуальный автоответчик: активируется по команде #помощь или на все вопросы. "
        "Использует Anthropic Messages API (прямой SDK или совместимый REST), учитывает лот и зовёт продавца при затруднении."
    ),
    "settings": {
        "api_key": {
            "label": "API Key",
            "type": "str",
            "default": "",
        },
        "base_url": {
            "label": "Base URL",
            "type": "str",
            "default": "https://api.anthropic.com",
        },
        "model": {
            "label": "ID Модели",
            "type": "str",
            "default": "claude-3-5-sonnet-20241022",
        },
        "base_prompt": {
            "label": "Базовый промпт",
            "type": "str",
            "default": (
                "Ты — дружелюбный и вежливый ИИ-ассистент продавца на маркетплейсе цифровых товаров Playerok. "
                "Твоя задача — помочь покупателю с его заказом, активацией товара, решением проблем или ответами на частые вопросы. "
                "Отвечай кратко, четко и по делу на русском языке. "
                "Если ты точно не знаешь ответа, если вопрос требует действий лично от продавца (возврат, ручная выдача, проверка чека), "
                "или если покупатель настаивает на человеке, обязательно включи в свой ответ точный тег [CALL_SELLER] и напиши, что зовёшь продавца."
            ),
        },
        "mode": {
            "label": "Режим работы",
            "type": "choice",
            "choices": ["help_command_only", "always_after_help", "all_messages"],
            "default": "help_command_only",
        },
        "notify_tg_on_reply": {
            "label": "Уведомлять в TG об ответах ИИ",
            "type": "bool",
            "default": True,
        },
    },
}

# Хранилище активных диалогов с ИИ в памяти: chat_id -> timestamp последнего обращения
_ACTIVE_AI_CHATS: dict[str, float] = {}


class AnthropicBridge:
    """Универсальный клиент Anthropic Messages API.
    
    Использует официальный пакет anthropic при его наличии в системе,
    либо встроенный urllib/requests клиент, полностью реализующий Messages API
    (поддерживает api.anthropic.com, OpenRouter, proxy и любые совместимые base_url).
    """

    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key.strip()
        base = (base_url or "https://api.anthropic.com").strip().rstrip("/")
        if not base.startswith("http://") and not base.startswith("https://"):
            base = f"https://{base}"
        self.base_url = base

    def _sync_http_request(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=45.0) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Anthropic API Error ({err.code}): {err_body[:400]}") from err

    async def create_message(
        self,
        model: str,
        system_prompt: str,
        messages: list[dict[str, str]],
        max_tokens: int = 1000,
    ) -> str:
        # 1. Попытка использовать установленный anthropic SDK
        try:
            import anthropic  # type: ignore
            client = anthropic.AsyncAnthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=45.0,
            )
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages,
            )
            chunks = []
            for block in response.content:
                if getattr(block, "text", None):
                    chunks.append(block.text)
            return "".join(chunks).strip()
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("Anthropic SDK call failed, falling back to direct HTTP: %s", exc)

        # 2. Прямой асинхронный HTTP-запрос к Anthropic Messages API через to_thread
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "Playerok-Anthropic-Plugin/1.0",
        }
        if "openrouter.ai" in self.base_url:
            headers["authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url}/v1/messages" if not self.base_url.endswith("/messages") else self.base_url

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
        }

        data = await asyncio.to_thread(self._sync_http_request, url, headers, payload)
        chunks = []
        for block in data.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])
        return "".join(chunks).strip()


def _extract_item_context(chat: Any, message: Any) -> str:
    """Извлекает информацию о лоте/сделке из контекста чата или сообщения."""
    lines = []
    
    # 1. Проверяем сделку из сообщения
    deal = getattr(message, "deal", None)
    item = getattr(message, "item", None)
    
    # 2. Если в сообщении нет, проверяем список сделок чата
    if not deal:
        deals = getattr(chat, "deals", []) or []
        if deals:
            deal = deals[-1]
            
    if deal and not item:
        item = getattr(deal, "item", None)

    if item:
        name = getattr(item, "name", None) or getattr(item, "title", None)
        desc = getattr(item, "description", None)
        price = getattr(item, "price", None)
        if name:
            lines.append(f"Товар: {name}")
        if price:
            lines.append(f"Цена: {price} ₽")
        if desc:
            clean_desc = str(desc).strip()
            if len(clean_desc) > 800:
                clean_desc = clean_desc[:800] + "..."
            lines.append(f"Описание лота: {clean_desc}")

    if deal:
        deal_id = getattr(deal, "id", None)
        status = getattr(deal, "status", None)
        status_name = getattr(status, "name", str(status)) if status else "—"
        obtaining = getattr(deal, "obtaining", None)
        if deal_id:
            lines.append(f"ID сделки: {deal_id}")
        lines.append(f"Статус сделки: {status_name}")
        if obtaining:
            lines.append(f"Способ получения: {obtaining}")

    game = getattr(message, "game", None) or getattr(chat, "game", None)
    if game and not lines:
        lines.append(f"Категория/Игра: {getattr(game, 'name', str(game))}")

    return "\n".join(lines) if lines else "Информация о конкретном лоте в чате отсутствует."


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    text = str(getattr(message, "text", "") or "").strip()
    if not text:
        return

    chat_id = str(getattr(chat, "id", ""))
    if not chat_id:
        return

    config = ctx.config
    api_key = str(config.get("api_key") or "").strip()
    if not api_key:
        return

    base_url = str(config.get("base_url") or "https://api.anthropic.com").strip()
    model = str(config.get("model") or "claude-3-5-sonnet-20241022").strip()
    base_prompt = str(config.get("base_prompt") or "").strip()
    mode = str(config.get("mode") or "help_command_only")

    is_help_command = text.startswith("#помощь") or text.startswith("#help")
    loop = asyncio.get_running_loop()
    now = loop.time()

    should_respond = False
    clean_user_question = text

    if is_help_command:
        should_respond = True
        _ACTIVE_AI_CHATS[chat_id] = now
        parts = text.split(maxsplit=1)
        clean_user_question = parts[1].strip() if len(parts) > 1 else "Здравствуйте, мне нужна помощь по заказу."
    elif mode == "all_messages":
        should_respond = True
    elif mode == "always_after_help":
        last_time = _ACTIVE_AI_CHATS.get(chat_id, 0)
        if now - last_time < 1800:
            should_respond = True

    if not should_respond:
        return

    sender_user = getattr(message, "user", None)
    buyer_name = getattr(sender_user, "username", "Покупатель") or "Покупатель"

    item_context = _extract_item_context(chat, message)

    system_instruction = (
        f"{base_prompt}\n\n"
        "--- КОНТЕКСТ ДАННОГО ЗАКАЗА И ЛОТА ---\n"
        f"{item_context}\n"
        f"Имя покупателя: {buyer_name}\n"
        "-------------------------------------\n"
        "ВАЖНО:\n"
        "1. Отвечай от лица поддержки продавца.\n"
        "2. Если ты не можешь помочь или вопрос требует действий продавца, добавь в ответ тег [CALL_SELLER] и напиши, что позвал продавца.\n"
        "3. Никогда не обещай возвратов и не подтверждай ничего от лица финансового отдела площадки."
    )

    client = AnthropicBridge(api_key=api_key, base_url=base_url)

    try:
        reply_text = await client.create_message(
            model=model,
            system_prompt=system_instruction,
            messages=[{"role": "user", "content": clean_user_question}],
            max_tokens=800,
        )
    except Exception as exc:
        logger.exception("AI assistant generation failed for chat %s", chat_id)
        await ctx.send_chat(
            chat_id,
            "🤖 Извините, произошла техническая ошибка связи с ИИ. Я уже уведомил продавца, он ответит вам в ближайшее время!",
        )
        await ctx.notify(
            f"⚠️ <b>ИИ Ассистент: ошибка вызова API</b> в чате с <b>@{buyer_name}</b>:\n"
            f"<code>{str(exc)[:400]}</code>\n"
            f"Вопрос покупателя: {clean_user_question}"
        )
        return

    if not reply_text:
        return

    needs_seller = "[CALL_SELLER]" in reply_text
    final_reply = reply_text.replace("[CALL_SELLER]", "").strip()

    await ctx.send_chat(chat_id, final_reply)

    if needs_seller:
        await ctx.notify(
            f"🔔 <b>ИИ Ассистент зовёт продавца!</b>\n"
            f"Чат с: <b>@{buyer_name}</b> (ID чата: <code>{chat_id}</code>)\n"
            f"Вопрос покупателя: <i>{clean_user_question}</i>\n\n"
            f"Ответ ИИ покупателю:\n{final_reply}"
        )
    elif config.get("notify_tg_on_reply"):
        await ctx.notify(
            f"🤖 <b>ИИ ответил покупателю @{buyer_name}</b>\n"
            f"Вопрос: <i>{clean_user_question}</i>\n\n"
            f"Ответ:\n{final_reply}"
        )
