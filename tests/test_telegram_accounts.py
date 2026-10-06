import pytest
import uuid
from app.telegram_accounts_manager import (
    extract_5digit_code,
    format_delivery_message,
    format_code_message,
    format_code_waiting_message,
    format_code_repeat_message,
    TelegramAccountLotRule,
    TelegramAccountStock,
    TelegramAccountDeal,
)


def test_no_forbidden_word_account_in_messages():
    """Проверяем, что в сообщениях покупателю категорически отсутствует слово 'аккаунт'/'account'."""
    delivery = format_delivery_message("Россия", "+79991234567")
    code_msg = format_code_message("12345")
    waiting_msg = format_code_waiting_message("+79991234567")
    repeat_msg = format_code_repeat_message("12345")

    for msg in (delivery, code_msg, waiting_msg, repeat_msg):
        lower = msg.lower()
        assert "аккаунт" not in lower, f"Слово 'аккаунт' обнаружено в сообщении: {msg}"
        assert "account" not in lower, f"Слово 'account' обнаружено в сообщении: {msg}"
        assert "акк" not in lower.split(), f"Слово 'акк' обнаружено в сообщении: {msg}"


def test_extract_5digit_code():
    """Проверяем извлечение 5-значного кода из различных форматов служебных сообщений Telegram."""
    assert extract_5digit_code("Telegram: Login code: 12345. Do not give this code to anyone.") == "12345"
    assert extract_5digit_code("Ваш проверочный код для входа: 98765.") == "98765"
    assert extract_5digit_code("Код подтверждения: 54321") == "54321"
    assert extract_5digit_code("Login code: 789 01") == "78901"
    assert extract_5digit_code("Login code: 234-56") == "23456"
    assert extract_5digit_code("Просто текст без цифр") is None
    assert extract_5digit_code("Номер телефона +79991234567 не код") is None
    assert extract_5digit_code("1234 — не 5 знаков") is None
    assert extract_5digit_code("123456 — 6 знаков") is None
