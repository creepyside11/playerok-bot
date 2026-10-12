"""
Модуль клиента ИИ для Playerok Bot:
Поддерживает работу с OpenAI-совместимыми API:
- Chat Completions (LLM) для текста, анализа, генерации лотов и настройки
- Images Generations для генерации продающих обложек и фото
"""

import asyncio
import base64
import json
import logging
from typing import Any
import urllib.error
import urllib.request

logger = logging.getLogger("ai_client")


class UniversalOpenAIClient:
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", model: str = "gpt-4o-mini"):
        self.api_key = (api_key or "").strip()
        base = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
        if not base.startswith("http://") and not base.startswith("https://"):
            base = f"https://{base}"
        self.base_url = base
        self.model = (model or "gpt-4o-mini").strip()

    def _sync_request(self, url: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Playerok-AI-Agent/2.0",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"OpenAI API Error {err.code}: {err_body[:400]}") from err
        except Exception as exc:
            raise RuntimeError(f"Сетевая ошибка при обращении к ИИ: {exc}") from exc

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.5,
        response_json: bool = False,
    ) -> str:
        if not self.api_key:
            raise ValueError("API-ключ для LLM не настроен.")

        url = f"{self.base_url}/chat/completions" if not self.base_url.endswith("/chat/completions") else self.base_url
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_json:
            payload["response_format"] = {"type": "json_object"}

        res = await asyncio.to_thread(self._sync_request, url, payload, 60.0)
        choices = res.get("choices", [])
        if not choices:
            raise RuntimeError("ИИ вернул пустой ответ.")
        msg = choices[0].get("message", {})
        return str(msg.get("content") or "").strip()


async def web_request(url: str, method: str = "GET", headers: dict[str, str] | None = None, data: str | None = None, timeout: float = 25.0) -> dict[str, Any]:
    """Выполняет веб-запрос к внешнему сайту или REST API конкурента/поставщика."""
    def _do():
        clean_url = url.strip()
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
        }
        if headers:
            hdrs.update(headers)
        body = data.encode("utf-8") if data else None
        req = urllib.request.Request(clean_url, data=body, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                content_text = raw_bytes.decode(charset, errors="replace")
                return {
                    "status": resp.status,
                    "url": resp.geturl(),
                    "content": content_text[:12000],
                }
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            return {
                "status": err.code,
                "error": f"HTTP {err.code}",
                "content": err_body[:4000],
            }
        except Exception as exc:
            return {"status": 0, "error": str(exc), "content": ""}

    return await asyncio.to_thread(_do)



class UniversalImageClient:
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", model: str = "gpt-2.5-image"):
        self.api_key = (api_key or "").strip()
        base = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
        if not base.startswith("http://") and not base.startswith("https://"):
            base = f"https://{base}"
        self.base_url = base
        self.model = (model or "gpt-2.5-image").strip()

    def _sync_generate(self, prompt: str, timeout: float = 60.0) -> bytes | None:
        url = f"{self.base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Playerok-Image-AI/2.0",
        }
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                items = res.get("data", [])
                if not items:
                    return None
                it = items[0]
                if "b64_json" in it:
                    return base64.b64decode(it["b64_json"])
                elif "url" in it:
                    img_req = urllib.request.Request(it["url"], headers={"User-Agent": "Playerok-Image-AI/2.0"})
                    with urllib.request.urlopen(img_req, timeout=30.0) as img_resp:
                        return img_resp.read()
                return None
        except Exception as exc:
            logger.warning("Не удалось сгенерировать изображение (%s): %s", self.model, exc)
            return None

    async def generate_image(self, prompt: str) -> bytes | None:
        if not self.api_key:
            return None
        return await asyncio.to_thread(self._sync_generate, prompt)
