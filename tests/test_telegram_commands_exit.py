"""Грамматика команд выхода."""
import pytest

from app.telegram_commands import (
    ASK_ALIASES, CODE, EXIT, EXIT_ALIASES, KNOWN_COMMANDS, NO_ALIASES, YES_ALIASES,
    is_command, is_known_command,
)

@pytest.mark.parametrize("text", ["#выход", "#ВЫХОД", "  #Выход ", "#logout", "#выйти"])
def test_exit_aliases(text):
    assert is_command(text, EXIT_ALIASES)

@pytest.mark.parametrize("text", ["#выход123", "#выходы", "выход", ""])
def test_exit_lookalikes_rejected(text):
    assert not is_command(text, EXIT_ALIASES)

def test_yes_and_no_are_distinct():
    assert is_command("#да", YES_ALIASES) and not is_command("#да", NO_ALIASES)
    assert is_command("#нет", NO_ALIASES) and not is_command("#нет", YES_ALIASES)
    assert not is_command("#да", EXIT_ALIASES)
    assert not is_command("#да", ASK_ALIASES)

def test_exit_is_not_a_code_command():
    assert not is_command(EXIT, (CODE,))
    assert not is_command("#выход", ASK_ALIASES)

def test_all_new_commands_are_known():
    for command in (EXIT, "#да", "#нет", "#logout", "#выйти", "#yes", "#no"):
        assert is_known_command(command), command
    assert not is_known_command("да")
    assert not is_known_command("нет, спасибо")
