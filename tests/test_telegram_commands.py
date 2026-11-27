import pytest

from app.telegram_commands import (
    ASK, ASK_ALIASES, ASK_STOP, CODE, KNOWN_COMMANDS, SELLER, SELLER_ALIASES,
    command_argument, is_command, is_known_command, normalize,
)

def test_normalize_collapses_whitespace_and_case():
    assert normalize("  #Код   ПОЖАЛУЙСТА ") == "#код пожалуйста"
    assert normalize(None) == ""

@pytest.mark.parametrize("text", ["#код", "  #Код  ", "#код пожалуйста", "#КОД\n"])
def test_code_command_matches(text):
    assert is_command(text, (CODE,))

@pytest.mark.parametrize("text", ["#код123", "#коды", "код", "", None, "#код-2"])
def test_code_command_rejects_lookalikes(text):
    assert not is_command(text, (CODE,))

@pytest.mark.parametrize("text", ["#аск", "#АСК", "#аск Сколько ждать?", "#помощь", "#help мне"])
def test_ask_aliases(text):
    assert is_command(text, ASK_ALIASES)

def test_stop_is_not_an_ask():
    """#аскстоп начинается с #аск, но не должен запускать помощника."""
    assert is_command(ASK_STOP, (ASK_STOP,))
    assert not is_command(ASK_STOP, ASK_ALIASES)
    assert not is_command("#аскстоп", (CODE,))

def test_seller_aliases():
    for text in (SELLER, "#seller", "#человек", "#продавец нужен срочно"):
        assert is_command(text, SELLER_ALIASES)

def test_every_command_is_known_but_plain_text_is_not():
    for command in KNOWN_COMMANDS:
        assert is_known_command(command), command
    assert not is_known_command("привет, когда код?")
    assert not is_known_command("")

def test_command_argument_keeps_original_case():
    assert command_argument("#аск Сколько ждать код?", ASK_ALIASES) == "Сколько ждать код?"
    assert command_argument("  #АСК   Почему не приходит?  ", ASK_ALIASES) == "Почему не приходит?"
    assert command_argument("#аск", ASK_ALIASES) == ""
    assert command_argument("#аскстоп", ASK_ALIASES) == ""
    assert command_argument("#продавец нужен человек", SELLER_ALIASES) == "нужен человек"
