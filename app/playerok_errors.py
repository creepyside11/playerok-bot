from __future__ import annotations

import re
from typing import Any

from playerokapi.exceptions import (
    BotCheckDetectedException, RequestFailedError, RequestPlayerokError, RequestSendingError, UnauthorizedError,
)


PRIVATE_VALUE = re.compile(
    r"(?i)\b(?:authorization|cookie|token|api[_ -]?(?:key|hash)|password)\b\s*[:=]\s*(?:(?:Bearer|Basic)\s+)?[\w./+%:=-]+|"
    r"(?<=://)[^/@\s]+:[^/@\s]+@"
)


def strip_error_detail(value: str) -> str:
    return " ".join(PRIVATE_VALUE.sub("[скрыто]", value).split())[:420]


def response_reasons(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return []
    reasons: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, list):
            for part in value[:8]:
                add(part)
        elif isinstance(value, str) and (message := strip_error_detail(value)) and message not in reasons:
            reasons.append(message)
        elif isinstance(value, dict):
            for field in ("message", "reason", "detail"):
                if field in value:
                    add(value[field])

    errors = body.get("errors")
    for entry in errors[:6] if isinstance(errors, list) else [errors]:
        if isinstance(entry, dict):
            extensions = entry.get("extensions")
            if isinstance(extensions, dict):
                for key in ("originalError", "response", "reason", "details", "validationErrors", "message"):
                    add(extensions.get(key))
        add(entry)
    for key in ("message", "error", "detail", "reason"):
        add(body.get(key))
    return reasons[:8]


def playerok_error_text(exc: Exception) -> str:
    if isinstance(exc, (BotCheckDetectedException, UnauthorizedError)):
        return "Playerok запросил проверку входа. Проверьте подключение аккаунта в боте."
    if isinstance(exc, RequestSendingError):
        return f"Сетевая ошибка при обращении к Playerok: {strip_error_detail(exc.error) or type(exc).__name__}."
    if isinstance(exc, (RequestPlayerokError, RequestFailedError)):
        try:
            body = exc.response.json()
        except (TypeError, ValueError):
            body = None
        messages = response_reasons(body)
        if isinstance(exc, RequestPlayerokError):
            if not messages and exc.error_message:
                messages.append(strip_error_detail(exc.error_message))
            if exc.error_code:
                messages.append(f"Код: {strip_error_detail(str(exc.error_code))}.")
        else:
            messages.insert(0, f"HTTP {exc.status_code} от Playerok.")
            raw = (exc.html_text or "").strip()
            if len(messages) == 1 and raw and "<" not in raw:
                messages.append(strip_error_detail(raw))
        return "\n".join(messages)[:1600] or "Playerok вернул ошибку без пояснения."
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return f"Playerok вернул неполный или неожиданный ответ ({type(exc).__name__})."
    return f"Не удалось завершить запрос ({type(exc).__name__})."
