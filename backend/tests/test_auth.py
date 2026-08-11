"""API key helper unit tests."""
from utils.auth import api_key_accepted, extract_api_key, PUBLIC_PATHS


def test_public_paths_include_health_warmup():
    assert "/health" in PUBLIC_PATHS
    assert "/warmup" in PUBLIC_PATHS


def test_extract_header_and_bearer():
    assert extract_api_key({"x-api-key": "abc"}) == "abc"
    assert extract_api_key({"authorization": "Bearer xyz"}) == "xyz"
    assert extract_api_key({}, "from-query") == "from-query"


def test_api_key_accepted():
    assert api_key_accepted("", "") is True
    assert api_key_accepted("k", "") is True
    assert api_key_accepted("", "secret") is False
    assert api_key_accepted("wrong", "secret") is False
    assert api_key_accepted("secret", "secret") is True
