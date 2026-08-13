"""API key helper unit tests."""
from utils.auth import (
    api_key_accepted,
    extract_api_key,
    is_public_request,
    PUBLIC_PATHS,
)


def test_public_paths_include_health_warmup():
    assert "/health" in PUBLIC_PATHS
    assert "/warmup" in PUBLIC_PATHS


def test_style_catalog_get_is_public():
    assert is_public_request("GET", "/styles/templates") is True
    assert is_public_request("GET", "/styles/templates/orange-bounce") is True
    assert is_public_request("GET", "/styles/fonts") is True
    assert is_public_request("GET", "/styles/stroke-previews") is True
    assert is_public_request("GET", "/styles/stroke-previews/0") is True
    assert is_public_request("HEAD", "/styles/templates") is True


def test_style_mutations_are_protected():
    assert is_public_request("POST", "/styles/templates") is False
    assert is_public_request("POST", "/styles/fonts") is False
    assert is_public_request("PUT", "/styles/templates/x") is False


def test_upload_is_protected():
    assert is_public_request("POST", "/upload") is False
    assert is_public_request("GET", "/process/abc") is False


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
    assert api_key_accepted("short", "longer-secret") is False
