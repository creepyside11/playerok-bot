from __future__ import annotations

import ast
import inspect
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID


try:
    import anthropic
except ImportError:  # The bot can still show a useful setup error before dependencies are installed.
    anthropic = None


DEFAULT_API_BASE_URL = "https://api.anthropic.com"
MAX_PLUGIN_REQUEST_LENGTH = 8_000
MAX_GENERATED_PLUGIN_BYTES = 512 * 1024
REQUIRED_PLUGIN_FIELDS = (
    "PLUGIN_META",
)


class AIPluginBuilderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GeneratedPlugin:
    source: str
    summary: str


def validate_api_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Base URL должен быть полным HTTPS-адресом без логина, query и fragment."
        )
    return normalized


def validate_model_id(value: str) -> str:
    model = value.strip()
    if not 2 <= len(model) <= 160 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", model):
        raise ValueError("ID модели должен содержать 2–160 латинских символов, цифр, '.', '_', ':', '/' или '-'.")
    return model


def validate_plugin_request(value: str) -> str:
    request = value.strip()
    if len(request) < 20:
        raise ValueError("Опишите плагин подробнее — минимум 20 символов.")
    if len(request) > MAX_PLUGIN_REQUEST_LENGTH:
        raise ValueError(f"Описание не должно превышать {MAX_PLUGIN_REQUEST_LENGTH} символов.")
    return request


def _literal_metadata(tree: ast.Module) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            try:
                values[target.id] = ast.literal_eval(value_node)
            except (TypeError, ValueError):
                pass
    return values


def inspect_generated_source(source: str, expected_id: str | None = None) -> dict[str, Any]:
    clean = source.strip().removeprefix("\ufeff") + "\n"
    if len(clean.encode("utf-8")) > MAX_GENERATED_PLUGIN_BYTES:
        raise AIPluginBuilderError("сгенерированный плагин превышает лимит 512 КБ")
    try:
        tree = ast.parse(clean, filename="generated_plugin.py")
    except SyntaxError as exc:
        location = f"строка {exc.lineno}" if exc.lineno else "неизвестная строка"
        raise AIPluginBuilderError(f"ошибка синтаксиса ({location}): {exc.msg}") from exc

    metadata = _literal_metadata(tree)
    meta = metadata.get("PLUGIN_META")
    if not isinstance(meta, dict):
        raise AIPluginBuilderError("обязательный словарь PLUGIN_META должен быть задан литералом")

    plugin_id = str(meta.get("id") or "").strip()
    if not plugin_id or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,32}", plugin_id):
        raise AIPluginBuilderError("PLUGIN_META['id'] должен содержать 1-32 латинских символа, цифры или . _ -")
    if expected_id and plugin_id != expected_id:
        raise AIPluginBuilderError("при редактировании модель изменила ID плагина")

    for field in ("name", "version", "description", "author"):
        if not isinstance(meta.get(field), str) or not meta[field].strip():
            raise AIPluginBuilderError(f"PLUGIN_META['{field}'] должен быть непустой строкой")

    forbidden_imports = {"subprocess", "ctypes"}
    forbidden_calls = {"eval", "exec", "compile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".", 1)[0] for alias in node.names}
            if imported & forbidden_imports:
                raise AIPluginBuilderError("автоматическая установка запрещает subprocess и ctypes")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".", 1)[0] in forbidden_imports:
                raise AIPluginBuilderError("автоматическая установка запрещает subprocess и ctypes")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in forbidden_calls:
                raise AIPluginBuilderError(f"автоматическая установка запрещает {node.func.id}()")

    return {
        "id": plugin_id,
        "name": meta["name"],
        "version": meta["version"],
        "description": meta["description"],
        "author": meta["author"],
        "source": clean,
    }


def generated_filename(name: str, plugin_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", plugin_id).strip("_")[:32]
    return f"{slug or 'ai_plugin'}.py"


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(str(block.text))
    text = "\n".join(parts).strip()
    if not text:
        raise AIPluginBuilderError("модель вернула пустой ответ")
    return text


def parse_generated_plugin(value: str) -> GeneratedPlugin:
    source_match = re.search(
        r"<plugin_source>\s*(.*?)\s*</plugin_source>", value, re.DOTALL | re.IGNORECASE
    )
    if not source_match:
        raise AIPluginBuilderError("модель не вернула блок <plugin_source>")
    source = source_match.group(1).strip()
    if source.startswith("```"):
        source = re.sub(r"^```(?:python|py)?\s*", "", source, flags=re.IGNORECASE)
        source = re.sub(r"\s*```$", "", source)
    summary_match = re.search(
        r"<summary>\s*(.*?)\s*</summary>", value, re.DOTALL | re.IGNORECASE
    )
    summary = summary_match.group(1).strip() if summary_match else "Плагин создан и проверен."
    if not source:
        raise AIPluginBuilderError("модель вернула пустой исходник плагина")
    return GeneratedPlugin(source=source + "\n", summary=summary[:3000])


def syntax_report(source: str) -> str:
    try:
        ast.parse(source, filename="draft_plugin.py")
    except SyntaxError as exc:
        return f"ОШИБКА: строка {exc.lineno}: {exc.msg}"
    return "Синтаксис Python корректен."


class AnthropicPluginBuilder:
    def __init__(self, api_key: str, base_url: str, model_id: str):
        if anthropic is None:
            raise AIPluginBuilderError(
                "пакет anthropic не установлен; установите зависимости из requirements.txt"
            )
        self.api_key = api_key
        self.base_url = validate_api_base_url(base_url)
        self.model_id = validate_model_id(model_id)

    async def _request(self, system: str, user: str) -> str:
        client = anthropic.AsyncAnthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=180,
            max_retries=1,
        )
        try:
            response = await client.messages.create(
                model=self.model_id,
                max_tokens=16_000,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return _response_text(response)
        except AIPluginBuilderError:
            raise
        except Exception as exc:
            raise AIPluginBuilderError(f"Anthropic API: {exc}") from exc
        finally:
            close_result = client.close()
            if inspect.isawaitable(close_result):
                await close_result

    @staticmethod
    def _system(documentation: str, *, review: bool) -> str:
        role = (
            "Ты старший ревьюер Python-плагинов Playerok. Исправь черновик"
            if review
            else "Ты разработчик однофайловых Python-плагинов Playerok. Создай готовый плагин"
        )
        return f"""{role} строго по приложенной официальной документации.

Правила безопасности:
- не читай и не отправляй BOT_TOKEN, DATABASE_URL, cookies, proxy и переменные окружения;
- не используй subprocess, ctypes, eval, exec, compile и shell-команды;
- не добавляй скрытую телеметрию, бэкдоры или действия вне запроса пользователя;
- все сетевые адреса и токены внешних сервисов должны настраиваться пользователем через PLUGIN_META["settings"];
- результат обязан быть полноценным UTF-8 Python-модулем до 512 КБ с PLUGIN_META и хуками on_message / on_deal / on_action / on_schedule.

Верни только два XML-блока, без текста снаружи:
<plugin_source>
полный исходник без Markdown-ограждения
</plugin_source>
<summary>
краткий понятный пользователю вывод на русском: что сделано и как пользоваться
</summary>

ОФИЦИАЛЬНАЯ ДОКУМЕНТАЦИЯ ПЛАГИНОВ:
---
{documentation}
---"""

    async def create_draft(self, request: str, documentation: str) -> GeneratedPlugin:
        request = validate_plugin_request(request)
        response = await self._request(
            self._system(documentation, review=False),
            "Создай новый плагин Playerok по запросу ниже. Задай уникальный английский id в PLUGIN_META, версию 1.0.0. "
            "Реализуй весь заявленный сценарий, настройки и обработку ошибок.\n\n"
            f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{request}",
        )
        return parse_generated_plugin(response)

    async def edit_draft(
        self,
        request: str,
        documentation: str,
        current_source: str,
        expected_id: str,
    ) -> GeneratedPlugin:
        request = validate_plugin_request(request)
        response = await self._request(
            self._system(documentation, review=False),
            "Измени существующий плагин по запросу. Сохрани PLUGIN_META['id'] без изменений, увеличь версию, "
            "не удаляй работающие возможности, если пользователь этого не просил.\n\n"
            f"ОБЯЗАТЕЛЬНЫЙ ID: {expected_id}\n"
            f"ЗАПРОС НА ИЗМЕНЕНИЕ:\n{request}\n\n"
            f"ТЕКУЩИЙ ИСХОДНИК:\n{current_source}",
        )
        return parse_generated_plugin(response)

    async def review_draft(
        self,
        request: str,
        documentation: str,
        draft: GeneratedPlugin,
        expected_id: str | None = None,
    ) -> GeneratedPlugin:
        response = await self._request(
            self._system(documentation, review=True),
            "Это этап 2 из 2. Проверь соответствие исходника запросу и документации Playerok, исправь синтаксис, "
            "контракт, обработку ошибок и безопасность. Верни полный окончательный исходник. "
            "Не меняй PLUGIN_META['id'] при редактировании.\n\n"
            f"ИСХОДНЫЙ ЗАПРОС:\n{request}\n\n"
            f"ОЖИДАЕМЫЙ ID: {expected_id or 'новый id из черновика'}\n"
            f"ЛОКАЛЬНАЯ ПРОВЕРКА ЧЕРНОВИКА: {syntax_report(draft.source)}\n\n"
            f"ЧЕРНОВИК:\n{draft.source}",
        )
        reviewed = parse_generated_plugin(response)
        inspect_generated_source(reviewed.source, expected_id=expected_id)
        return reviewed
