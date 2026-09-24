import pytest
from app.emerald_promo_manager import (
    validate_api_url,
    validate_token_amount,
    format_tokens,
    promo_prefix,
    free_message,
    sale_message,
    review_message,
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

    msg_rev = review_message("sk-em-test123", 100000, is_key=True)
    assert "БОНУС ЗА ОТЗЫВ 5★" in msg_rev
    assert "sk-em-test123" in msg_rev

    msg_free = free_message("sk-em-free", 200000, is_key=True)
    assert "ВАШ БЕСПЛАТНЫЙ ДОСТУП" in msg_free
    assert "sk-em-free" in msg_free
