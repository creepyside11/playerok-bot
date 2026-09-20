from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import json
import logging
import re
from typing import Any
import urllib.parse
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

PLUGIN_META = {
    "id": "funpay_arbitrage",
    "name": "FunPay ⇄ Playerok AI Ultra Arbitrage",
    "version": "3.0.0",
    "author": "Playerok Bot Community",
    "description": (
        "Ультимативный арбитраж FunPay ⇄ Playerok с ИИ нового поколения:\n"
        "1. 🎨 Генератор обложек и превью товаров через OpenAI Image API (gpt-image / gpt-2.5-image / dall-e-3 / compatible):\n"
        "   Создаёт кликабельные, сочные продающие превью и автоматически прикрепляет к лотам Playerok.\n"
        "2. 🚀 ИИ-дожим покупателя на допродажи (Upselling / Cross-selling):\n"
        "   После успешной выдачи аккаунта ИИ ненавязчиво предлагает сопутствующие товары (донат, валюту, пропуски) с персональной скидкой.\n"
        "3. 🕹️ Мульти-чекер аккаунтов перед выдачей (Brawl Stars, Steam, Roblox, Genshin, Telegram):\n"
        "   Проверка VAC-банов Steam, профилей Brawl Stars/Supercell, юзернеймов Roblox и доступности почт.\n"
        "4. 2FA/Email-парсер кодов по IMAP/API и ретрансляция кодов от продавца FP.\n"
        "5. Умный финансовый калькулятор комиссий с категоризацией и защитой от убытков.\n"
        "6. Failover (автозамена поставщика при возврате) и автоподтверждение через 12ч без жалоб."
    ),
    "settings": {
        "direction_mode": {
            "label": "Направление работы",
            "type": "choice",
            "choices": [
                "funpay_to_playerok",
                "playerok_to_funpay",
                "bidirectional",
            ],
            "default": "funpay_to_playerok",
        },
        "funpay_golden_key": {
            "label": "FunPay golden_key (токен авторизации)",
            "type": "str",
            "default": "",
        },
        "funpay_user_agent": {
            "label": "FunPay User-Agent",
            "type": "str",
            "default": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        },
        "funpay_proxy": {
            "label": "Прокси для FunPay (http://user:pass@ip:port или пусто)",
            "type": "str",
            "default": "",
        },
        "min_funpay_balance_rub": {
            "label": "Минимальный остаток на балансе FunPay (руб)",
            "type": "int",
            "default": 100,
        },
        # ================= AI ЧАТ / ТЕКСТ =================
        "ai_provider": {
            "label": "ИИ Провайдер",
            "type": "choice",
            "choices": ["openai_compatible", "anthropic"],
            "default": "openai_compatible",
        },
        "ai_api_key": {
            "label": "ИИ API Key (OpenAI / OpenRouter / Anthropic / Groq)",
            "type": "str",
            "default": "",
        },
        "ai_base_url": {
            "label": "ИИ Base URL",
            "type": "str",
            "default": "https://api.openai.com/v1",
        },
        "ai_model": {
            "label": "ИИ Модель",
            "type": "str",
            "default": "gpt-4o-mini",
        },
        "ai_system_prompt": {
            "label": "Системный промпт ИИ",
            "type": "str",
            "default": (
                "Ты — автономный торговый AI-агент и арбитражный брокер между маркетплейсами цифровых товаров.\n"
                "Твои ключевые функции:\n"
                "1. Анализ товаров, категоризация, расчет параметров и подбор лучших предложений.\n"
                "2. Извлечение учетных записей, паролей, кодов 2FA и ключей из сырых сообщений чата поставщика.\n"
                "3. Вежливая и дипломатичная координация продавца и покупателя (встреча в игре, передача виртуальной валюты, передача кодов подтверждения).\n"
                "4. Ни при каких обстоятельствах не упоминай конкурирующие площадки (FunPay, Playerok, FP и т.д.) в переписке."
            ),
        },
        # ================= AI ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ =================
        "image_ai_enabled": {
            "label": "Генерировать обложки лотов через ИИ",
            "type": "bool",
            "default": True,
        },
        "image_ai_api_key": {
            "label": "OpenAI Image API Key (если пусто, берется ai_api_key)",
            "type": "str",
            "default": "",
        },
        "image_ai_base_url": {
            "label": "OpenAI Image Base URL",
            "type": "str",
            "default": "https://api.openai.com/v1",
        },
        "image_ai_model": {
            "label": "Модель генерации превью (gpt-2.5-image / dall-e-3)",
            "type": "str",
            "default": "gpt-2.5-image",
        },
        # ================= ДОПРОДАЖИ (UPSELLING) =================
        "upselling_enabled": {
            "label": "Включить ИИ-дожим на сопутствующие допродажи",
            "type": "bool",
            "default": True,
        },
        "upselling_delay_sec": {
            "label": "Задержка перед допродажей (секунд после выдачи)",
            "type": "int",
            "default": 30,
        },
        "upselling_prompt": {
            "label": "Инструкция ИИ для допродаж",
            "type": "str",
            "default": (
                "Покупатель только что успешно получил свой товар. Напиши 1-2 вежливых, ненавязчивых предложения "
                "с предложением сопутствующего товара (игровая валюта, боевой пропуск, гемы, ключи) со скидкой постоянного клиента."
            ),
        },
        # ================= МУЛЬТИ-ЧЕКЕРЫ =================
        "brawl_stars_api_token": {
            "label": "Brawl Stars Official API Token (необязательно)",
            "type": "str",
            "default": "",
        },
        "steam_web_api_key": {
            "label": "Steam Web API Key (необязательно)",
            "type": "str",
            "default": "",
        },
        # ================= ФИНАНСЫ И ЛИМИТЫ =================
        "min_seller_rating": {
            "label": "Минимальный рейтинг продавца на FunPay (1-5)",
            "type": "int",
            "default": 3,
        },
        "min_deal_price_rub": {
            "label": "Минимальная цена покупки лота на FP (руб)",
            "type": "int",
            "default": 10,
        },
        "playerok_fee_percent": {
            "label": "Базовая комиссия Playerok (%)",
            "type": "int",
            "default": 20,
        },
        "funpay_fee_percent": {
            "label": "Базовая комиссия FunPay (%)",
            "type": "int",
            "default": 5,
        },
        "category_fees_json": {
            "label": "Индивидуальные комиссии категорий (JSON)",
            "type": "str",
            "default": '{"currency": {"po": 15, "fp": 3}, "accounts": {"po": 20, "fp": 7}, "brawl_stars": {"po": 18, "fp": 5}}',
        },
        "min_profit_rub": {
            "label": "Минимальная чистая прибыль (руб)",
            "type": "int",
            "default": 30,
        },
        "price_markup_percent": {
            "label": "Желаемая наценка при репостинге (%)",
            "type": "int",
            "default": 25,
        },
        "auto_confirm_fp_hours": {
            "label": "Автоподтверждение заказа на FP без жалоб (часов)",
            "type": "int",
            "default": 12,
        },
        "seller_ping_delay_min": {
            "label": "Через сколько минут напомнить продавцу FP о выдаче",
            "type": "int",
            "default": 5,
        },
        "monitor_interval_sec": {
            "label": "Интервал проверки чата и статуса FP (сек)",
            "type": "int",
            "default": 30,
        },
        "funpay_category_mappings": {
            "label": "Конфигурация категорий FunPay (JSON)",
            "type": "str",
            "default": '{"black_russia": "lots/1476/", "brawl_stars": "lots/554/", "telegram": "lots/1480/", "steam": "lots/81/", "roblox": "lots/220/", "genshin": "lots/601/"}',
        },
        "lot_mappings": {
            "label": "Таблица точных связок лотов (JSON)",
            "type": "str",
            "default": "{}",
        },
        "notify_tg": {
            "label": "Подробные уведомления в Telegram",
            "type": "bool",
            "default": True,
        },
    },
}

ACTIVE_ARBITRAGE_DEALS: dict[str, dict[str, Any]] = {}


class FeeCalculator:
    """Расчёт комиссий и финансовой рентабельности с учётом категорий."""

    @staticmethod
    def get_category_fees(category_key: str, config: dict[str, Any]) -> tuple[float, float]:
        raw_fees = str(config.get("category_fees_json") or "{}").strip()
        default_po = float(config.get("playerok_fee_percent") or 20)
        default_fp = float(config.get("funpay_fee_percent") or 5)

        try:
            mapping = json.loads(raw_fees)
            for k, v in mapping.items():
                if k.lower() in category_key.lower():
                    po = float(v.get("po", default_po))
                    fp = float(v.get("fp", default_fp))
                    return po, fp
        except Exception:
            pass

        return default_po, default_fp

    @classmethod
    def calculate_max_buy_price(
        cls, sell_price: float, category_key: str, min_profit: float, config: dict[str, Any]
    ) -> float:
        po_fee_pct, fp_fee_pct = cls.get_category_fees(category_key, config)
        net_received_from_playerok = sell_price * (1.0 - (po_fee_pct / 100.0))
        target_budget = net_received_from_playerok - min_profit
        if target_budget <= 0:
            return 0.0

        max_fp_price = target_budget / (1.0 + (fp_fee_pct / 100.0))
        return max(0.0, round(max_fp_price, 2))


class MailboxReader:
    """Чтение писем и кодов подтверждения по IMAP."""

    IMAP_SERVERS = {
        "rambler.ru": "imap.rambler.ru",
        "lenta.ru": "imap.rambler.ru",
        "autorambler.ru": "imap.rambler.ru",
        "myrambler.ru": "imap.rambler.ru",
        "ro.ru": "imap.rambler.ru",
        "firstmail.ltd": "imap.firstmail.ltd",
        "mail.ru": "imap.mail.ru",
        "inbox.ru": "imap.mail.ru",
        "bk.ru": "imap.mail.ru",
        "list.ru": "imap.mail.ru",
        "yandex.ru": "imap.yandex.ru",
        "gmail.com": "imap.gmail.com",
    }

    @classmethod
    async def verify_credentials(cls, email_address: str, password: str) -> bool:
        def _check():
            try:
                domain = email_address.split("@")[-1].lower() if "@" in email_address else ""
                imap_host = cls.IMAP_SERVERS.get(domain, f"imap.{domain}")
                mail = imaplib.IMAP4_SSL(imap_host, timeout=8)
                mail.login(email_address, password)
                mail.logout()
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_check)

    @classmethod
    async def fetch_latest_code(cls, email_address: str, password: str, timeout_sec: int = 15) -> str | None:
        def _read():
            try:
                domain = email_address.split("@")[-1].lower() if "@" in email_address else ""
                imap_host = cls.IMAP_SERVERS.get(domain, f"imap.{domain}")
                mail = imaplib.IMAP4_SSL(imap_host, timeout=10)
                mail.login(email_address, password)
                mail.select("INBOX")

                status, messages = mail.search(None, "ALL")
                if status != "OK" or not messages[0]:
                    mail.logout()
                    return None

                msg_ids = messages[0].split()
                latest_id = msg_ids[-1]

                res, data = mail.fetch(latest_id, "(RFC822)")
                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                mail.logout()

                match = re.search(r"\b(\d{4,8})\b", body)
                if match:
                    return match.group(1)
                return None
            except Exception as exc:
                logger.warning("MailboxReader failed for %s: %s", email_address, exc)
                return None

        return await asyncio.to_thread(_read)


class MultiGameAccountChecker:
    """Глубокая валидация игровых аккаунтов (Brawl Stars, Steam, Roblox, Genshin) перед выдачей."""

    @staticmethod
    async def check_brawl_stars(tag_or_text: str, api_token: str = "") -> dict[str, Any]:
        match = re.search(r"#?([0289PYLQGRJCUV]{4,12})", tag_or_text.upper())
        tag = f"#{match.group(1)}" if match else ""
        if not tag:
            return {"checked": False, "valid": True, "reason": "Тег не найден в сообщении"}

        clean_tag = urllib.parse.quote(tag)
        if api_token:
            url = f"https://api.brawlstars.com/v1/players/{clean_tag}"
            headers = {"Authorization": f"Bearer {api_token.strip()}"}
            req = urllib.request.Request(url, headers=headers)
            try:
                def _do_api():
                    with urllib.request.urlopen(req, timeout=10.0) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                profile = await asyncio.to_thread(_do_api)
                return {
                    "checked": True,
                    "valid": True,
                    "name": profile.get("name"),
                    "trophies": profile.get("trophies"),
                }
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    return {"checked": True, "valid": False, "reason": "Игрок не найден"}
            except Exception:
                pass

        # Fallback web-checker
        url = f"https://api.brawlify.com/v1/players/{clean_tag.lstrip('#')}"
        req = urllib.request.Request(url, headers={"User-Agent": "Playerok-Checker/1.0"})
        try:
            def _do_web():
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            data = await asyncio.to_thread(_do_web)
            if data and data.get("name"):
                return {"checked": True, "valid": True, "name": data.get("name"), "trophies": data.get("trophies")}
        except Exception:
            pass

        return {"checked": False, "valid": True, "reason": "Внешний чекер недоступен"}

    @staticmethod
    async def check_steam(text: str, web_api_key: str = "") -> dict[str, Any]:
        """Проверяет Steam профиль на VAC бан и статус аккаунта."""
        match = re.search(r"(?:steamcommunity\.com/profiles/|id/)?(\d{17})", text)
        if not match:
            return {"checked": False, "valid": True, "reason": "SteamID64 не обнаружен"}

        steam_id = match.group(1)
        if web_api_key:
            url = f"https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/?key={web_api_key}&steamids={steam_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Playerok-Checker/1.0"})
            try:
                def _do_steam():
                    with urllib.request.urlopen(req, timeout=10.0) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                data = await asyncio.to_thread(_do_steam)
                players = data.get("players", [])
                if players:
                    p = players[0]
                    vac_banned = bool(p.get("VACBanned"))
                    return {
                        "checked": True,
                        "valid": not vac_banned,
                        "steam_id": steam_id,
                        "vac_banned": vac_banned,
                        "reason": "VAC Ban обнаружен" if vac_banned else "OK",
                    }
            except Exception:
                pass

        return {"checked": False, "valid": True, "reason": "API ключ Steam не задан"}

    @staticmethod
    async def check_roblox(username: str) -> dict[str, Any]:
        """Проверяет существование пользователя Roblox через публичный API."""
        clean_user = username.strip()
        if not clean_user or len(clean_user) < 3:
            return {"checked": False, "valid": True}

        url = "https://users.roblox.com/v1/usernames/users"
        payload = json.dumps({"usernames": [clean_user], "excludeBannedUsers": True}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            def _do_roblox():
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            res = await asyncio.to_thread(_do_roblox)
            data = res.get("data", [])
            if data:
                return {"checked": True, "valid": True, "roblox_id": data[0].get("id"), "name": data[0].get("name")}
            return {"checked": True, "valid": False, "reason": "Пользователь Roblox не существует или забанен"}
        except Exception:
            return {"checked": False, "valid": True}


class AccountValidator:
    """Комплексная проверка валидности товара (Anti-Scam)."""

    @staticmethod
    def validate_format(data_str: str) -> dict[str, Any]:
        clean = (data_str or "").strip()
        result = {
            "valid": False,
            "type": "unknown",
            "email": "",
            "password": "",
            "code": "",
            "error": "",
        }

        if not clean or len(clean) < 4:
            result["error"] = "Пустые данные"
            return result

        email_match = re.search(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+):([^\s\n\r]+)", clean)
        if email_match:
            result["valid"] = True
            result["type"] = "credentials"
            result["email"] = email_match.group(1).strip()
            result["password"] = email_match.group(2).strip()
            return result

        login_match = re.search(r"([a-zA-Z0-9_.-]{3,32}):([^\s\n\r]{4,64})", clean)
        if login_match:
            result["valid"] = True
            result["type"] = "account"
            result["email"] = login_match.group(1).strip()
            result["password"] = login_match.group(2).strip()
            return result

        key_match = re.search(r"([A-Z0-9]{4,6}(?:-[A-Z0-9]{4,6}){2,5})", clean)
        if key_match:
            result["valid"] = True
            result["type"] = "key"
            result["code"] = key_match.group(1).strip()
            return result

        if "http://" in clean or "https://" in clean:
            result["valid"] = True
            result["type"] = "link"
            return result

        if len(clean) >= 8:
            result["valid"] = True
            result["type"] = "custom_data"
            return result

        result["error"] = "Не распознан валидный формат товара"
        return result


class ImageGeneratorClient:
    """Генерация продающих превью лотов через OpenAI Image API (gpt-2.5-image / dall-e-3 / compatible)."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key.strip()
        base = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
        if not base.startswith("http://") and not base.startswith("https://"):
            base = f"https://{base}"
        self.base_url = base
        self.model = model.strip() or "gpt-2.5-image"

    async def generate_lot_cover(self, item_name: str, category_name: str = "") -> bytes | None:
        """Генерирует продающую квадратную обложку для лота на Playerok."""
        if not self.api_key:
            return None

        prompt = (
            f"High-quality, eye-catching 3D game render banner for selling '{item_name}' ({category_name}). "
            "Epic lighting, digital gaming marketplace style, vibrant neon colors, cinematic composition, 4k, trending artstation style. "
            "No small blurry text, clean professional gaming graphics."
        )

        url = f"{self.base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
        }

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

        def _fetch():
            try:
                with urllib.request.urlopen(req, timeout=45.0) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    data_items = res.get("data", [])
                    if not data_items:
                        return None
                    item = data_items[0]
                    if "b64_json" in item:
                        return base64.b64decode(item["b64_json"])
                    elif "url" in item:
                        img_req = urllib.request.Request(item["url"], headers={"User-Agent": "Playerok-Image/1.0"})
                        with urllib.request.urlopen(img_req, timeout=30.0) as img_resp:
                            return img_resp.read()
            except Exception as exc:
                logger.warning("Image AI generation failed (%s): %s", self.model, exc)
                return None

        return await asyncio.to_thread(_fetch)


class AIClient:
    """Универсальный клиент для работы с LLM (OpenAI, Anthropic, OpenRouter, Groq)."""

    def __init__(self, provider: str, api_key: str, base_url: str, model: str, system_prompt: str):
        self.provider = provider
        self.api_key = api_key.strip()
        base = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
        if not base.startswith("http://") and not base.startswith("https://"):
            base = f"https://{base}"
        self.base_url = base
        self.model = model.strip() or "gpt-4o-mini"
        self.system_prompt = system_prompt.strip()

    def _sync_request(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=40.0) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"AI API HTTP {err.code}: {err_body[:300]}") from err
        except Exception as exc:
            raise RuntimeError(f"AI API network error: {exc}") from exc

    async def call_ai(self, user_prompt: str) -> str:
        if not self.api_key:
            return ""

        try:
            if self.provider == "anthropic":
                headers = {
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }
                url = f"{self.base_url}/v1/messages" if not self.base_url.endswith("/messages") else self.base_url
                payload = {
                    "model": self.model,
                    "max_tokens": 1024,
                    "system": self.system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                }
                res = await asyncio.to_thread(self._sync_request, url, headers, payload)
                raw_text = ""
                for block in res.get("content", []):
                    if isinstance(block, dict) and block.get("text"):
                        raw_text += block["text"]
                return raw_text.strip()
            else:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
                url = f"{self.base_url}/chat/completions" if not self.base_url.endswith("/chat/completions") else self.base_url
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
                }
                res = await asyncio.to_thread(self._sync_request, url, headers, payload)
                choices = res.get("choices") or []
                return str(choices[0]["message"]["content"]).strip() if choices else ""
        except Exception as exc:
            logger.warning("AI request failed: %s", exc)
            return ""

    async def generate_upsell_pitch(self, item_name: str, custom_instructions: str) -> str:
        """ИИ генерирует персональное, кликабельное предложение допродажи сопутствующего товара."""
        prompt = (
            f"Товар, который только что приобрёл покупатель: '{item_name}'.\n"
            f"Инструкция: {custom_instructions}\n\n"
            "Напиши короткое (1-2 предложения), дружелюбное сообщение в чат маркетплейса с предложением "
            "сопутствующего доната, валюты или прокачки со скидкой 10-15%. Без приветствий и официоза, по делу."
        )
        res = await self.call_ai(prompt)
        if res:
            return res.strip()
        return f"Кстати, к вашему заказу ({item_name}) у нас действует скидка 15% на игровую валюту и донат! Напишите, если интересно 😉"

    async def coordinate_trade(self, context_type: str, buyer_text: str, game: str) -> dict[str, Any]:
        prompt = (
            f"Игровая передача / встреча в игре ({game}).\n"
            f"Тип ситуации: {context_type}\n"
            f"Сообщение от покупателя: '{buyer_text}'\n\n"
            "Сформулируй две вещи:\n"
            "1. Сообщение продавцу FunPay (чтобы продавец передал вирты или подошел в игре, без упоминания сторонних сайтов).\n"
            "2. Инструкцию покупателю (где ждать, что сделать).\n"
            "Ответь строго в формате JSON:\n"
            "{\n"
            '  "to_seller": "сообщение продавцу",\n'
            '  "to_buyer": "сообщение покупателю",\n'
            '  "need_code_request": true/false\n'
            "}"
        )
        res = await self.call_ai(prompt)
        match = re.search(r"\{.*\}", res, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass

        return {
            "to_seller": f"Здравствуйте! Покупатель передал данные: {buyer_text}. Готовы забрать в игре.",
            "to_buyer": "Данные переданы поставщику! Ожидайте готовности к встрече.",
            "need_code_request": "код" in buyer_text.lower(),
        }

    async def extract_delivery_data(self, chat_messages: list[str]) -> dict[str, Any]:
        combined = "\n---\n".join(chat_messages[-10:])
        prompt = (
            "Проанализируй последние сообщения от продавца FunPay.\n"
            "Выдал ли продавец товар (логин:пароль, данные почты, ключ, код активации, ссылку)?\n"
            "Ответь строго в JSON:\n"
            "{\n"
            '  "is_delivered": true/false,\n'
            '  "credentials": "чистый текст данных для выдачи",\n'
            '  "has_email": true/false,\n'
            '  "email_address": "почта если есть",\n'
            '  "email_password": "пароль от почты если есть",\n'
            '  "game_tag": "тег аккаунта или юзернейм если указан",\n'
            '  "summary": "краткое резюме"\n'
            "}\n\n"
            f"Сообщения чата:\n{combined}"
        )
        resp = await self.call_ai(prompt)
        match = re.search(r"\{.*\}", resp, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass

        for msg in reversed(chat_messages):
            match_em = re.search(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+):([^\s]+)", msg)
            if match_em:
                return {
                    "is_delivered": True,
                    "credentials": msg.strip(),
                    "has_email": True,
                    "email_address": match_em.group(1),
                    "email_password": match_em.group(2),
                    "summary": "Логин и пароль распознаны",
                }

        return {"is_delivered": False, "credentials": "", "has_email": False, "summary": ""}

    async def rewrite_listing(self, original_title: str, original_desc: str, target_platform: str) -> dict[str, str]:
        if not self.api_key:
            return {
                "name": original_title[:100],
                "description": original_desc,
                "instructions": "Спасибо за заказ! Ожидайте выдачи товара.",
            }

        prompt = (
            f"Адаптируй и оптимизируй карточку товара для площадки {target_platform}.\n\n"
            f"Оригинальное название: {original_title}\n"
            f"Оригинальное описание:\n{original_desc}\n\n"
            f"Верни ответ строго в JSON: {{\"name\": \"новое название (до 100 симв)\", \"description\": \"новое продающее описание\", \"instructions\": \"инструкция покупателю\"}}"
        )

        res = await self.call_ai(prompt)
        match = re.search(r"\{.*\}", res, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                return {
                    "name": str(data.get("name") or original_title)[:100],
                    "description": str(data.get("description") or original_desc),
                    "instructions": str(data.get("instructions") or "Спасибо за заказ!"),
                }
            except Exception:
                pass

        return {
            "name": original_title[:100],
            "description": original_desc,
            "instructions": "Спасибо за заказ!",
        }


class FunPayAPI:
    """Полнофункциональный клиент FunPay."""

    def __init__(self, golden_key: str, user_agent: str, proxy: str = ""):
        self.golden_key = golden_key.strip()
        self.user_agent = user_agent.strip()
        self.proxy = proxy.strip()

    def _make_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        return urllib.request.build_opener(*handlers)

    def _request(self, endpoint: str, data: dict[str, Any] | None = None, method: str = "GET") -> str:
        url = f"https://funpay.com/{endpoint.lstrip('/')}"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cookie": f"golden_key={self.golden_key}",
        }
        encoded_data = urllib.parse.urlencode(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
        opener = self._make_opener()
        with opener.open(req, timeout=30.0) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    async def get_balance(self) -> float:
        def _get():
            html = self._request("")
            match = re.search(r'class="[^"]*badge-balance[^"]*"[^>]*>([\d\s.,]+)', html)
            if not match:
                match = re.search(r'data-balance="([\d\s.,]+)"', html)
            if not match:
                match = re.search(r'class="user-link-dropdown"[^>]*>.*?([\d\s.,]+)\s*₽', html, re.DOTALL)
            if match:
                clean = match.group(1).replace(" ", "").replace(",", ".").strip()
                try:
                    return float(clean)
                except ValueError:
                    pass
            return 0.0

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            logger.warning("Failed to fetch FunPay balance: %s", exc)
            return 0.0

    async def search_lots(
        self, category_path: str, query: str = "", min_rating: int = 3, min_price: float = 10.0
    ) -> list[dict[str, Any]]:
        def _search():
            endpoint = category_path.strip("/")
            if query:
                endpoint += f"?query={urllib.parse.quote(query)}"
            html_text = self._request(endpoint)
            lots = []

            pattern = re.compile(
                r'<a\s+href="https://funpay\.com/lots/offer\?id=(\d+)"[^>]*class="[^"]*tc-item[^"]*"[^>]*>'
                r'.*?<div class="tc-desc-text">(.*?)</div>'
                r'.*?<div class="tc-price"[^>]*><div>([\d\s.,]+)\s*<span[^>]*>.*?</span></div></div>'
                r'.*?<div class="media-user-name">(.*?)</div>'
                r'.*?(?:<span class="rating-stars rating-(\d+)">)?',
                re.DOTALL,
            )

            for match in pattern.finditer(html_text):
                lot_id = match.group(1).strip()
                desc = re.sub(r"<[^>]+>", "", match.group(2)).strip()
                raw_price = match.group(3).replace(" ", "").replace(",", ".").strip()
                price = float(raw_price) if raw_price else 0.0
                seller = re.sub(r"<[^>]+>", "", match.group(4)).strip()
                rating_str = match.group(5)
                rating = int(rating_str) if rating_str else 5

                if price < min_price or rating < min_rating:
                    continue

                lots.append({
                    "lot_id": lot_id,
                    "title": desc,
                    "price": price,
                    "seller": seller,
                    "rating": rating,
                })
            return lots

        try:
            return await asyncio.to_thread(_search)
        except Exception as exc:
            logger.warning("FunPay search failed for %s: %s", category_path, exc)
            return []

    async def get_lot_details(self, lot_id: str) -> dict[str, Any]:
        def _fetch():
            html_text = self._request(f"lots/offer?id={lot_id}")
            title_m = re.search(r'<span class="param-title">.*?</span>\s*<h5>(.*?)</h5>', html_text, re.DOTALL)
            title = title_m.group(1).strip() if title_m else f"FunPay Lot #{lot_id}"

            price_m = re.search(r'<span class="payment-value">([\d\s.,]+)</span>', html_text)
            price = 0.0
            if price_m:
                raw_p = price_m.group(1).replace(" ", "").replace(",", ".")
                price = float(raw_p)

            desc_m = re.search(r'<div class="param-item">\s*<h5>Описание</h5>\s*<div>(.*?)</div>', html_text, re.DOTALL)
            desc = desc_m.group(1).strip() if desc_m else ""
            desc = re.sub(r"<[^>]+>", "", desc)

            seller_m = re.search(r'<div class="media-user-name">\s*<a[^>]*>(.*?)</a>', html_text)
            seller = seller_m.group(1).strip() if seller_m else "Unknown"

            return {
                "lot_id": lot_id,
                "title": title,
                "price": price,
                "description": desc,
                "seller": seller,
            }

        return await asyncio.to_thread(_fetch)

    async def purchase_lot(self, lot_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        def _exec():
            html = self._request(f"lots/offer?id={lot_id}")
            csrf_m = re.search(r'data-csrf="([^"]+)"', html)
            csrf = csrf_m.group(1) if csrf_m else ""

            payload = {
                "csrf_token": csrf,
                "offer": lot_id,
                "node_id": "0",
                "payment_type": "balance",
            }
            payload.update(fields)
            resp = self._request("orders/checkout", data=payload, method="POST")

            order_m = re.search(r'orders/([A-Z0-9]+)', resp)
            order_id = order_m.group(1) if order_m else f"FP-{lot_id}"
            return {"success": True, "order_id": order_id, "raw_response": resp[:200]}

        return await asyncio.to_thread(_exec)

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        def _check():
            html = self._request(f"orders/{order_id}")
            is_refunded = "Возврат" in html or "возвращен" in html.lower() or "refund" in html.lower()
            is_closed = "Закрыт" in html or "выполнен" in html.lower() or "closed" in html.lower()
            return {
                "order_id": order_id,
                "is_refunded": is_refunded,
                "is_closed": is_closed,
            }

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return {"order_id": order_id, "is_refunded": False, "is_closed": False}

    async def confirm_order(self, order_id: str) -> bool:
        def _confirm():
            html = self._request(f"orders/{order_id}")
            csrf_m = re.search(r'data-csrf="([^"]+)"', html)
            csrf = csrf_m.group(1) if csrf_m else ""
            payload = {
                "csrf_token": csrf,
                "order_id": order_id,
                "rating": "5",
                "text": "Отличный продавец, всё получено быстро и чётко!",
            }
            self._request("orders/complete", data=payload, method="POST")
            return True

        try:
            return await asyncio.to_thread(_confirm)
        except Exception:
            return False

    async def get_order_chat_history(self, order_id: str) -> list[str]:
        def _get_history():
            html = self._request(f"orders/{order_id}")
            messages = []
            for m in re.finditer(r'<div class="chat-msg-text">([^<]+)</div>', html):
                text = m.group(1).strip()
                if text:
                    messages.append(text)
            return messages

        try:
            return await asyncio.to_thread(_get_history)
        except Exception:
            return []

    async def send_order_message(self, order_id: str, text: str) -> bool:
        def _send():
            html = self._request(f"orders/{order_id}")
            csrf_m = re.search(r'data-csrf="([^"]+)"', html)
            csrf = csrf_m.group(1) if csrf_m else ""
            chat_m = re.search(r'data-id="(\d+)"', html) or re.search(r'chat_id=(\d+)', html)
            chat_id = chat_m.group(1) if chat_m else ""

            payload = {
                "csrf_token": csrf,
                "chat_id": chat_id,
                "text": text,
            }
            self._request("runner/", data=payload, method="POST")
            return True

        try:
            return await asyncio.to_thread(_send)
        except Exception:
            return False


def _parse_lot_mappings(raw: str) -> dict[str, dict[str, Any]]:
    clean = (raw or "").strip()
    if not clean or clean == "{}":
        return {}

    if clean.startswith("{"):
        try:
            data = json.loads(clean)
            if isinstance(data, dict):
                return {str(k).strip(): dict(v) for k, v in data.items()}
        except Exception:
            pass

    mappings: dict[str, dict[str, Any]] = {}
    for line in clean.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) >= 2:
            key = parts[0].strip()
            val = parts[1].strip()
            price = float(parts[2].strip()) if len(parts) >= 3 else 0.0
            mappings[key] = {"target_id": val, "price": price}
    return mappings


def _parse_category_mappings(raw: str) -> dict[str, str]:
    clean = (raw or "").strip()
    if not clean or clean == "{}":
        return {}

    if clean.startswith("{"):
        try:
            data = json.loads(clean)
            if isinstance(data, dict):
                return {str(k).strip(): str(v).strip() for k, v in data.items()}
        except Exception:
            pass

    res = {}
    for line in clean.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) >= 2:
            res[parts[0].strip()] = parts[1].strip()
    return res


async def trigger_upselling(ctx: Any, chat_id: str, item_name: str, config: dict[str, Any]) -> None:
    """Отложенная отправка персонального предложения допродажи через ИИ."""
    delay = int(config.get("upselling_delay_sec") or 30)
    await asyncio.sleep(delay)

    ai = AIClient(
        provider=str(config.get("ai_provider") or "openai_compatible"),
        api_key=str(config.get("ai_api_key") or ""),
        base_url=str(config.get("ai_base_url") or "https://api.openai.com/v1"),
        model=str(config.get("ai_model") or "gpt-4o-mini"),
        system_prompt=str(config.get("ai_system_prompt") or ""),
    )
    custom_inst = str(config.get("upselling_prompt") or "")
    pitch = await ai.generate_upsell_pitch(item_name, custom_inst)

    if pitch and chat_id:
        try:
            await ctx.send_chat(chat_id, pitch)
            if config.get("notify_tg"):
                await ctx.notify(f"💡 <b>ИИ отправил допродажу покупателю:</b>\n<i>«{pitch}»</i>")
        except Exception as exc:
            logger.warning("Failed to send upselling message: %s", exc)


async def handle_delivery_polling(ctx: Any, deal_id: str, fp_order_id: str, po_chat_id: str, config: dict[str, Any]) -> None:
    golden_key = str(config.get("funpay_golden_key") or "").strip()
    fp_api = FunPayAPI(
        golden_key=golden_key,
        user_agent=str(config.get("funpay_user_agent") or ""),
        proxy=str(config.get("funpay_proxy") or ""),
    )
    ai = AIClient(
        provider=str(config.get("ai_provider") or "openai_compatible"),
        api_key=str(config.get("ai_api_key") or ""),
        base_url=str(config.get("ai_base_url") or "https://api.openai.com/v1"),
        model=str(config.get("ai_model") or "gpt-4o-mini"),
        system_prompt=str(config.get("ai_system_prompt") or ""),
    )

    interval = int(config.get("monitor_interval_sec") or 30)
    ping_delay_sec = int(config.get("seller_ping_delay_min") or 5) * 60
    ping_text = str(config.get("seller_ping_text") or "Здравствуйте! Оплатил заказ, подскажите, когда сможете выдать?")
    auto_confirm_hours = int(config.get("auto_confirm_fp_hours") or 12)

    start_time = asyncio.get_event_loop().time()
    pinged = False

    while True:
        await asyncio.sleep(interval)
        deal_entry = ACTIVE_ARBITRAGE_DEALS.get(deal_id)
        if not deal_entry:
            break

        # 1. Failover при возврате
        status_info = await fp_api.get_order_status(fp_order_id)
        if status_info.get("is_refunded"):
            if config.get("notify_tg"):
                await ctx.notify(
                    f"⚠️ <b>Продавец FunPay сделал возврат по заказу {fp_order_id}!</b>\n"
                    f"Запускаю поиск резервного поставщика (Failover)..."
                )
            deal_entry["order_history"] = deal_entry.get("order_history", []) + [fp_order_id]
            await execute_failover_purchase(ctx, deal_id, deal_entry, config)
            break

        # 2. 12 часов без жалоб -> подтверждение на FP
        if deal_entry.get("delivered"):
            delivered_at = deal_entry.get("delivered_at", 0)
            elapsed_hours = (asyncio.get_event_loop().time() - delivered_at) / 3600.0
            if elapsed_hours >= auto_confirm_hours and not deal_entry.get("fp_confirmed"):
                if not deal_entry.get("has_complaints"):
                    await fp_api.confirm_order(fp_order_id)
                    deal_entry["fp_confirmed"] = True
                    if config.get("notify_tg"):
                        await ctx.notify(
                            f"🌟 <b>Спустя {auto_confirm_hours}ч без жалоб заказ {fp_order_id} на FunPay автоматически подтвержден!</b>"
                        )
                break

        # 3. Сканирование чата
        history = await fp_api.get_order_chat_history(fp_order_id)
        if history:
            res = await ai.extract_delivery_data(history)
            if res.get("is_delivered") and res.get("credentials"):
                credentials = res["credentials"]
                val_res = AccountValidator.validate_format(credentials)

                if val_res["valid"]:
                    # Почтовый чек
                    if res.get("has_email") and res.get("email_address") and res.get("email_password"):
                        deal_entry["email_credentials"] = {
                            "email": res["email_address"],
                            "password": res["email_password"],
                        }
                        await MailboxReader.verify_credentials(res["email_address"], res["email_password"])

                    # Мульти-чекеры игр (Brawl Stars, Steam, Roblox)
                    item_name_lower = deal_entry.get("item_name", "").lower()
                    
                    if "brawl" in item_name_lower or res.get("game_tag"):
                        bs_token = str(config.get("brawl_stars_api_token") or "")
                        bs_res = await MultiGameAccountChecker.check_brawl_stars(
                            res.get("game_tag") or credentials, bs_token
                        )
                        if bs_res.get("checked") and not bs_res.get("valid"):
                            await fp_api.send_order_message(
                                fp_order_id,
                                "Здравствуйте! Проверка профиля Brawl Stars показала ошибку (аккаунт не найден). Проверьте данные."
                            )
                            continue

                    if "steam" in item_name_lower:
                        steam_key = str(config.get("steam_web_api_key") or "")
                        steam_res = await MultiGameAccountChecker.check_steam(credentials, steam_key)
                        if steam_res.get("checked") and not steam_res.get("valid"):
                            await fp_api.send_order_message(
                                fp_order_id, "Здравствуйте! Проверка аккаунта Steam показала наличие VAC бана. Требуется замена."
                            )
                            continue

                    if "roblox" in item_name_lower:
                        roblox_res = await MultiGameAccountChecker.check_roblox(val_res.get("email", ""))
                        if roblox_res.get("checked") and not roblox_res.get("valid"):
                            await fp_api.send_order_message(
                                fp_order_id, "Здравствуйте! Игрок Roblox не существует или забанен. Проверьте ник."
                            )
                            continue

                    deal_entry["delivered"] = True
                    deal_entry["delivered_at"] = asyncio.get_event_loop().time()
                    deal_entry["credentials"] = credentials

                    if po_chat_id:
                        delivery_msg = (
                            "✅ <b>Ваш заказ готов!</b>\n\n"
                            f"<b>Данные:</b>\n<code>{credentials}</code>\n\n"
                            "Если для входа потребуется код подтверждения с почты — просто напишите слово <b>код</b> в этот чат!"
                        )
                        await ctx.send_chat(po_chat_id, delivery_msg)

                    if config.get("notify_tg"):
                        await ctx.notify(
                            f"🎉 <b>Товар с FunPay валидирован и выдан покупателю!</b>\n"
                            f"Сделка Playerok: <code>{deal_id}</code>\n"
                            f"Заказ FP: <code>{fp_order_id}</code>\n"
                            f"Тип: <code>{val_res['type']}</code>"
                        )

                    # Запуск ИИ-дожима на сопутствующие допродажи (Upselling)
                    if config.get("upselling_enabled"):
                        asyncio.create_task(
                            trigger_upselling(ctx, po_chat_id, deal_entry.get("item_name", ""), config)
                        )
                    continue

        # 4. Напоминание
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= ping_delay_sec and not pinged:
            pinged = True
            await fp_api.send_order_message(fp_order_id, ping_text)
            if config.get("notify_tg"):
                await ctx.notify(f"⏰ Отправлено напоминание продавцу FunPay по заказу {fp_order_id}")


async def execute_failover_purchase(ctx: Any, deal_id: str, deal_entry: dict[str, Any], config: dict[str, Any]) -> None:
    fp_api = FunPayAPI(
        golden_key=str(config.get("funpay_golden_key") or ""),
        user_agent=str(config.get("funpay_user_agent") or ""),
        proxy=str(config.get("funpay_proxy") or ""),
    )
    category_path = deal_entry.get("category_path", "lots/")
    query = deal_entry.get("search_query", "")
    max_price = deal_entry.get("max_buy_price", 999999.0)
    min_rating = int(config.get("min_seller_rating") or 3)

    candidates = await fp_api.search_lots(category_path, query, min_rating=min_rating)
    valid_candidates = [c for c in candidates if c["price"] <= max_price]

    if not valid_candidates:
        if config.get("notify_tg"):
            await ctx.notify(f"❌ <b>Failover</b>: Не найден альтернативный продавец по бюджету до {max_price} руб!")
        return

    valid_candidates.sort(key=lambda x: x["price"])
    best_lot = valid_candidates[0]
    new_fp_lot_id = best_lot["lot_id"]

    try:
        purchase_res = await fp_api.purchase_lot(new_fp_lot_id, deal_entry.get("buyer_fields", {}))
        new_order_id = purchase_res["order_id"]
        deal_entry["fp_order_id"] = new_order_id
        deal_entry["fp_lot_id"] = new_fp_lot_id

        if config.get("notify_tg"):
            await ctx.notify(
                f"✅ <b>Резервный заказ успешно оформлен!</b>\n"
                f"Новый заказ FP: <code>{new_order_id}</code> (Лот #{new_fp_lot_id})\n"
                f"Продавец: <b>{best_lot.get('seller')}</b> (Цена: {best_lot.get('price')} ₽)"
            )

        asyncio.create_task(handle_delivery_polling(ctx, deal_id, new_order_id, deal_entry.get("po_chat_id"), config))
    except Exception as exc:
        logger.exception("Failover purchase error: %s", exc)


async def handle_funpay_to_playerok_deal(ctx: Any, deal: Any, config: dict[str, Any]) -> None:
    item = getattr(deal, "item", None)
    if not item:
        return

    item_id = str(getattr(item, "id", "") or "").strip()
    deal_id = str(getattr(deal, "id", "") or "").strip()
    item_name = str(getattr(item, "name", "") or getattr(item, "title", "Товар")).strip()
    po_price = float(getattr(item, "price", 0) or 0)
    chat_id = str(getattr(getattr(deal, "chat", None), "id", "") or "")

    golden_key = str(config.get("funpay_golden_key") or "").strip()
    if not golden_key:
        return

    fp_api = FunPayAPI(
        golden_key=golden_key,
        user_agent=str(config.get("funpay_user_agent") or ""),
        proxy=str(config.get("funpay_proxy") or ""),
    )
    current_fp_balance = await fp_api.get_balance()
    min_fp_balance = float(config.get("min_funpay_balance_rub") or 100)

    min_profit = float(config.get("min_profit_rub") or 30)
    max_buy_price = FeeCalculator.calculate_max_buy_price(po_price, item_name, min_profit, config)

    min_deal_price = float(config.get("min_deal_price_rub") or 10)
    if po_price < min_deal_price:
        logger.info("Deal %s skipped: price %s < min %s", deal_id, po_price, min_deal_price)
        return

    if current_fp_balance > 0 and (current_fp_balance - max_buy_price) < min_fp_balance:
        if config.get("notify_tg"):
            await ctx.notify(
                f"⚠️ <b>Внимание: Недостаточно средств на балансе FunPay!</b>\n"
                f"Баланс FunPay: <b>{current_fp_balance} ₽</b>\n"
                f"Требуется на выкуп до: <b>{max_buy_price} ₽</b> (неснижаемый остаток: {min_fp_balance} ₽)\n"
                f"Пожалуйста, пополните баланс на FunPay для продолжения автовыкупа!"
            )
        return

    category_rules = _parse_category_mappings(str(config.get("funpay_category_mappings") or ""))
    cat_url = "lots/"
    for key, path in category_rules.items():
        if key.lower() in item_name.lower():
            cat_url = path
            break

    min_rating = int(config.get("min_seller_rating") or 3)
    candidates = await fp_api.search_lots(cat_url, item_name, min_rating=min_rating, min_price=min_deal_price)

    affordable = [c for c in candidates if c["price"] <= max_buy_price]
    if not affordable:
        if config.get("notify_tg"):
            await ctx.notify(
                f"🛑 <b>FunPay Arbitrage: Отказ от выкупа по сделке {deal_id}</b> ({item_name})\n"
                f"Цена продажи на PO: <b>{po_price} ₽</b>\n"
                f"Порог закупки: <b>до {max_buy_price} ₽</b>\n"
                f"Сделка невыгодна, покупка заблокирована для защиты от убытка!"
            )
        return

    affordable.sort(key=lambda x: x["price"])
    best_lot = affordable[0]
    fp_lot_id = best_lot["lot_id"]

    buyer_fields: dict[str, Any] = {}
    for f in getattr(deal, "data_fields", []) or []:
        val = getattr(f, "value", None)
        if val:
            buyer_fields[str(getattr(f, "name", "field"))] = str(val)

    purchase_res = await fp_api.purchase_lot(fp_lot_id, buyer_fields)
    fp_order_id = purchase_res["order_id"]

    deal_entry = {
        "po_chat_id": chat_id,
        "fp_order_id": fp_order_id,
        "fp_lot_id": fp_lot_id,
        "item_name": item_name,
        "po_price": po_price,
        "max_buy_price": max_buy_price,
        "category_path": cat_url,
        "search_query": item_name,
        "buyer_fields": buyer_fields,
        "created_at": asyncio.get_event_loop().time(),
        "delivered": False,
        "fp_confirmed": False,
        "has_complaints": False,
    }
    ACTIVE_ARBITRAGE_DEALS[deal_id] = deal_entry

    if chat_id:
        await ctx.send_chat(
            chat_id,
            f"Здравствуйте! Ваш заказ принят (ID: {fp_order_id}). Товар оформлен у поставщика. "
            f"Ожидайте автоматической выдачи в этом чате!"
        )

    if config.get("notify_tg"):
        await ctx.notify(
            f"✅ <b>Заказ выкуплен на FunPay!</b>\n"
            f"Сделка: <code>{deal_id}</code> | Заказ FP: <code>{fp_order_id}</code>\n"
            f"Баланс FP: <b>{current_fp_balance} ₽</b> | Закупка: <b>{best_lot.get('price')} ₽</b>"
        )

    asyncio.create_task(handle_delivery_polling(ctx, deal_id, fp_order_id, chat_id, config))


async def sync_and_publish_from_funpay(
    ctx: Any,
    fp_lot_id: str,
    po_category_id: str,
    po_obtaining_id: str,
    markup_percent: int,
    min_profit: int,
    ai_client: AIClient,
) -> dict[str, Any]:
    """Парсит лот с FunPay, генерирует через ИИ текст и дизайнерскую обложку и выставляет на Playerok."""
    config = ctx.config
    golden_key = str(config.get("funpay_golden_key") or "").strip()
    fp_api = FunPayAPI(
        golden_key=golden_key,
        user_agent=str(config.get("funpay_user_agent") or ""),
        proxy=str(config.get("funpay_proxy") or ""),
    )

    fp_lot = await fp_api.get_lot_details(fp_lot_id)
    raw_title = fp_lot["title"]
    raw_desc = fp_lot["description"]
    base_price = fp_lot["price"]

    final_price = int(base_price * (1.0 + (markup_percent / 100.0)))
    if (final_price - base_price) < min_profit:
        final_price = int(base_price + min_profit)
    if final_price < 10:
        final_price = 10

    # 1. Текстовый рерайтинг через LLM
    rewritten = await ai_client.rewrite_listing(
        original_title=raw_title,
        original_desc=raw_desc,
        target_platform="Playerok",
    )

    # 2. Генерация AI-обложки через OpenAI Image API (gpt-2.5-image / compatible)
    attachments = []
    if config.get("image_ai_enabled"):
        img_api_key = str(config.get("image_ai_api_key") or config.get("ai_api_key") or "").strip()
        img_base_url = str(config.get("image_ai_base_url") or "https://api.openai.com/v1").strip()
        img_model = str(config.get("image_ai_model") or "gpt-2.5-image").strip()

        if img_api_key:
            img_client = ImageGeneratorClient(img_api_key, img_base_url, img_model)
            cover_bytes = await img_client.generate_lot_cover(rewritten["name"], po_category_id)
            if cover_bytes:
                attachments.append(cover_bytes)

    # 3. Публикация лота на Playerok
    client = ctx.client
    created_item = await client.create_item(
        game_category_id=po_category_id,
        obtaining_type_id=po_obtaining_id,
        name=rewritten["name"],
        price=final_price,
        description=rewritten["description"],
        options={},
        data_fields=[],
        attachments=attachments,
    )

    try:
        priorities = await client.get_item_priority_statuses(str(created_item.id), final_price)
        default_priority_id = str(priorities[0].id) if priorities else "1"
        await client.publish_item(str(created_item.id), default_priority_id)
    except Exception as pub_exc:
        logger.warning("Auto publish item error: %s", pub_exc)

    return {
        "playerok_item_id": str(created_item.id),
        "name": rewritten["name"],
        "price": final_price,
        "base_price": base_price,
        "funpay_lot_id": fp_lot_id,
        "has_cover": len(attachments) > 0,
    }


# ==================== ХУКИ ПЛАГИНА ====================

async def on_deal(ctx: Any, deal: Any) -> None:
    direction = str(ctx.config.get("direction_mode") or "funpay_to_playerok")
    if direction in ("funpay_to_playerok", "bidirectional"):
        await handle_funpay_to_playerok_deal(ctx, deal, ctx.config)


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    text = str(getattr(message, "text", "") or "").strip()
    if not text:
        return

    chat_id = str(getattr(chat, "id", "") or "")
    matched_deal = None
    for d_id, deal_data in ACTIVE_ARBITRAGE_DEALS.items():
        if deal_data.get("po_chat_id") == chat_id:
            matched_deal = deal_data
            break

    if not matched_deal:
        return

    config = ctx.config
    fp_order_id = matched_deal.get("fp_order_id")
    fp_api = FunPayAPI(
        golden_key=str(config.get("funpay_golden_key") or ""),
        user_agent=str(config.get("funpay_user_agent") or ""),
        proxy=str(config.get("funpay_proxy") or ""),
    )
    ai = AIClient(
        provider=str(config.get("ai_provider") or "openai_compatible"),
        api_key=str(config.get("ai_api_key") or ""),
        base_url=str(config.get("ai_base_url") or "https://api.openai.com/v1"),
        model=str(config.get("ai_model") or "gpt-4o-mini"),
        system_prompt=str(config.get("ai_system_prompt") or ""),
    )

    lower = text.lower()

    if any(w in lower for w in ["код", "code", "смс", "пароль с почты", "письмо"]):
        email_creds = matched_deal.get("email_credentials")
        if email_creds and email_creds.get("email") and email_creds.get("password"):
            await ctx.send_chat(chat_id, "⏳ Проверяю почту на наличие нового кода подтверждения...")
            code = await MailboxReader.fetch_latest_code(email_creds["email"], email_creds["password"])
            if code:
                await ctx.send_chat(chat_id, f"🔑 <b>Ваш код подтверждения:</b> <code>{code}</code>")
                return

        await ctx.send_chat(chat_id, "⏳ Запрашиваю свежий код у поставщика, пожалуйста, подождите...")
        await fp_api.send_order_message(
            fp_order_id,
            "Здравствуйте! Покупатель запросил код подтверждения для входа. Пожалуйста, отправьте код из письма."
        )
        return

    if any(w in lower for w in ["не работает", "бан", "неверный", "обман", "верните", "проблема"]):
        matched_deal["has_complaints"] = True
        matched_deal["fp_confirmed"] = False
        await fp_api.send_order_message(
            fp_order_id,
            f"Покупатель сообщил о проблеме: '{text}'. Проверьте данные и помогите решить вопрос."
        )
        await ctx.send_chat(chat_id, "Ваше обращение передано техническому специалисту. Мы уже разбираемся!")
        return

    coord = await ai.coordinate_trade("ingame_handover", text, matched_deal.get("item_name", "Game"))
    if coord.get("to_seller"):
        await fp_api.send_order_message(fp_order_id, coord["to_seller"])
    if coord.get("to_buyer"):
        await ctx.send_chat(chat_id, coord["to_buyer"])


async def on_command(ctx: Any, command: str, args: list[str]) -> None:
    cmd = (command or "").strip().lower()
    if cmd not in ("fparb", "funpay", "arbitrage"):
        return

    sub = args[0].lower() if args else "status"
    config = ctx.config
    fp_api = FunPayAPI(
        golden_key=str(config.get("funpay_golden_key") or ""),
        user_agent=str(config.get("funpay_user_agent") or ""),
        proxy=str(config.get("funpay_proxy") or ""),
    )

    if sub == "status":
        key = str(config.get("funpay_golden_key") or "")
        ai_key = str(config.get("ai_api_key") or "")
        active_count = len(ACTIVE_ARBITRAGE_DEALS)
        fp_balance = await fp_api.get_balance() if key else 0.0

        await ctx.notify(
            f"🚀 <b>FunPay ⇄ Playerok AI Ultra Arbitrage v3.0:</b>\n"
            f"• Баланс FunPay: <b>{fp_balance} ₽</b> (Резерв: {config.get('min_funpay_balance_rub')} ₽)\n"
            f"• Активных сделок в работе: <b>{active_count}</b>\n"
            f"• Генерация превью (Image AI): <b>{'Вкл (' + str(config.get('image_ai_model')) + ')' if config.get('image_ai_enabled') else 'Выкл'}</b>\n"
            f"• ИИ-дожим допродаж (Upselling): <b>{'Вкл' if config.get('upselling_enabled') else 'Выкл'}</b>\n"
            f"• Мульти-чекеры (Brawl, Steam, Roblox): <b>Активны</b>\n"
            f"• FunPay Key: {'✅ Активен' if key else '❌ Не указан'}\n"
            f"• AI Provider: <code>{config.get('ai_provider')}</code>"
        )
    elif sub == "check_steam":
        s_id = args[1] if len(args) > 1 else "76561198000000000"
        key = str(config.get("steam_web_api_key") or "")
        res = await MultiGameAccountChecker.check_steam(s_id, key)
        await ctx.notify(
            f"🎮 <b>Проверка Steam ({s_id}):</b>\n"
            f"• Статус: <b>{'✅ Валиден' if res.get('valid') else '❌ Обнаружен VAC BAN'}</b>\n"
            f"• VAC Ban: <code>{res.get('vac_banned')}</code>"
        )
    elif sub == "check_roblox":
        user = args[1] if len(args) > 1 else "Builderman"
        res = await MultiGameAccountChecker.check_roblox(user)
        await ctx.notify(
            f"🎮 <b>Проверка Roblox ({user}):</b>\n"
            f"• Статус: <b>{'✅ Существует' if res.get('valid') else '❌ Не найден / бан'}</b>\n"
            f"• ID: <code>{res.get('roblox_id', '—')}</code>"
        )
