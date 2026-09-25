import pytest
from app.emerald_promo_manager import (
    TELEGRAPH_DOCS_URL,
    fetch_models_text,
    format_tokens,
    free_message,
    promo_prefix,
    review_message,
    sale_message,
    validate_api_url,
    validate_token_amount,
)

def test_validate_api_url():
    assert validate_api_url("https://www.emeraldai.sbs/seller/v1/") == "https://www.emeraldai.sbs/seller/v1"
    with pytest.raises(ValueError):
        validate_api_url("http://insecure.url")

def test_validate_token_amount():
    assert validate_token_amount("100 000") == 100000
    assert validate_token_amount(50000) == 50000
    with pytest.raises(ValueError):
        validate_token_amount(500)  # below 10 000

def test_format_tokens():
    assert format_tokens(1000000) == "1 000 000"

def test_promo_prefix():
    p = promo_prefix("acc1", "sale", "order123")
    assert p.startswith("S")
    assert len(p) == 10

def test_messages():
    msg = sale_message("sk-em-test123", 50000, 1, True, 100000, is_key=True)
    assert "sk-em-test123" in msg
    assert "50 000" in msg
    assert "100 000" in msg
    assert "#баланс" in msg
    assert "#модели" in msg
    assert TELEGRAPH_DOCS_URL in msg
    # Проверяем, что нет ссылок на сторонние веб-сайты
    assert "https://emeraldai.beer\n" not in msg

    msg_rev = review_message("sk-em-test123", 100000, is_key=True)
    assert "БОНУС ЗА ОТЗЫВ 5★" in msg_rev
    assert "sk-em-test123" in msg_rev
    assert TELEGRAPH_DOCS_URL in msg_rev
    assert "#баланс" in msg_rev
    assert "#модели" in msg_rev

    msg_free = free_message("sk-em-free", 200000, is_key=True)
    assert "ВАШ БЕСПЛАТНЫЙ ДОСТУП" in msg_free
    assert "sk-em-free" in msg_free
    assert TELEGRAPH_DOCS_URL in msg_free
    assert "#баланс" in msg_free
    assert "#модели" in msg_free


def test_fetch_models_text():
    text = fetch_models_text()
    assert "СПИСОК МОДЕЛЕЙ И СТАТУС" in text
    assert "claude-fable-5-1" in text
    assert "gpt-5-6-sol" in text
    assert "deepseek-v4" in text
    assert TELEGRAPH_DOCS_URL in text
    assert "#баланс" in text
    assert "🟢" in text
