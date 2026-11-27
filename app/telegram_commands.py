"""Buyer-facing chat commands shared by the Telegram plugins.

Both the stock plugin and the AI plugin receive every chat message, so the
command grammar lives in one place: a message like ``#код`` must keep working
while an AI dialog is active, and must never be answered by the assistant.
"""

from __future__ import annotations

from typing import Any

CODE = "#код"
ASK = "#аск"
ASK_STOP = "#аскстоп"
SELLER = "#продавец"
EXIT = "#выход"
YES = "#да"
NO = "#нет"

# «#помощь» и «#help» остались от первой версии плагина ИИ.
ASK_ALIASES = (ASK, "#помощь", "#help")
SELLER_ALIASES = (SELLER, "#seller", "#человек")
EXIT_ALIASES = (EXIT, "#logout", "#выйти")
YES_ALIASES = (YES, "#yes", "#ага")
NO_ALIASES = (NO, "#no", "#отмена")
#: Every known command. The assistant answers only the ask aliases and stays
#: silent on the rest, so ordinary commands keep working during a dialog.
KNOWN_COMMANDS = (
    CODE, ASK_STOP, *ASK_ALIASES, *SELLER_ALIASES, *EXIT_ALIASES, *YES_ALIASES, *NO_ALIASES,
)


def normalize(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def is_command(text: Any, aliases: tuple[str, ...]) -> bool:
    """True for ``#аск`` and for ``#аск вопрос`` — but not for ``#аскстоп``."""
    value = normalize(text)
    return any(value == alias or value.startswith(alias + " ") for alias in aliases)


def is_known_command(text: Any) -> bool:
    return is_command(text, KNOWN_COMMANDS)


def command_argument(text: Any, aliases: tuple[str, ...]) -> str:
    """Everything after the matched command, with the original casing kept."""
    raw = " ".join(str(text or "").split())
    lowered = raw.casefold()
    for alias in aliases:
        if lowered == alias:
            return ""
        if lowered.startswith(alias + " "):
            return raw[len(alias):].strip()
    return ""
