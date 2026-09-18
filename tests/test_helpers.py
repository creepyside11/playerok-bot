from app.playerok import normalize_proxy, parse_cookie_header


def test_proxy_normalization() -> None:
    assert normalize_proxy("1.2.3.4:8000") == "1.2.3.4:8000"
    assert normalize_proxy("1.2.3.4:8000:user:pass") == "user:pass@1.2.3.4:8000"
    assert normalize_proxy("-") is None


def test_cookie_parser() -> None:
    assert parse_cookie_header("token=abc; x=1") == {"token": "abc", "x": "1"}
