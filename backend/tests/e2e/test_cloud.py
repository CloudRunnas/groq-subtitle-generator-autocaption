"""Cloud E2E against the deployed Autocaption API (CloudFront).

Skipped unless E2E_BASE_URL is set (CI after CDK deploy).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

BASE = (os.environ.get("E2E_BASE_URL") or "").rstrip("/")
ORIGIN = (os.environ.get("E2E_ORIGIN") or "").rstrip("/")
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-central-1"
SECRET_ID = os.environ.get("E2E_API_KEY_SECRET", "autocaption/backend-api-key")

pytestmark = pytest.mark.skipif(not BASE, reason="E2E_BASE_URL not set")


def _origin() -> str:
    if ORIGIN:
        return ORIGIN
    if BASE.startswith("https://") and "/api" in BASE:
        return BASE.split("/api")[0]
    return BASE


def _api_key() -> str:
    env_key = (os.environ.get("E2E_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        import boto3

        sm = boto3.client("secretsmanager", region_name=AWS_REGION)
        raw = sm.get_secret_value(SecretId=SECRET_ID)["SecretString"]
        data = json.loads(raw) if raw.strip().startswith("{") else {"apiKey": raw}
        return (data.get("apiKey") or data.get("api_key") or "").strip()
    except Exception as exc:  # pragma: no cover - missing AWS in some envs
        pytest.skip(f"Could not load API key: {exc}")
        return ""


def _request(
    path: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    data: bytes | None = None,
    timeout: int = 30,
):
    url = f"{BASE}{path}"
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, dict(resp.headers.items()), body
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers.items()), err.read()


def _acao_values(headers: dict) -> list[str]:
    found = []
    for key, value in headers.items():
        if key.lower() == "access-control-allow-origin":
            found.extend([part.strip() for part in value.split(",") if part.strip()])
            # Also treat a single header that already contains a comma-list as invalid later
            if "," in value:
                found.append(value.strip())
    return found


def _content_type(headers: dict) -> str:
    for key, value in headers.items():
        if key.lower() == "content-type":
            return value
    return ""


def _assert_json_body(headers: dict, body: bytes) -> dict:
    ctype = _content_type(headers)
    snippet = body[:240]
    assert "application/json" in ctype.lower(), (
        f"expected application/json, got {ctype!r} body={snippet!r}"
    )
    assert not body.lstrip().startswith(b"<!DOCTYPE"), (
        f"API returned HTML instead of JSON: {snippet!r}"
    )
    return json.loads(body.decode())


def _assert_cors_ok(headers: dict, origin: str) -> None:
    raw = []
    for key, value in headers.items():
        if key.lower() == "access-control-allow-origin":
            raw.append(value)
    assert len(raw) <= 1, f"duplicate ACAO headers: {raw}"
    if not raw:
        # Same-origin CloudFront /api often still emits ACAO because FastAPI CORS is on.
        return
    value = raw[0].strip()
    assert "," not in value, f"invalid combined ACAO: {value!r}"
    assert value in ("*", origin), f"ACAO {value!r} does not match origin {origin!r}"


def test_health_ok():
    status, headers, body = _request("/health", headers={"Origin": _origin()})
    assert status == 200, body
    payload = _assert_json_body(headers, body)
    assert payload.get("status") == "ok"
    _assert_cors_ok(headers, _origin())


def test_templates_list_public_and_cors():
    origin = _origin()
    status, headers, body = _request("/styles/templates", headers={"Origin": origin})
    assert status == 200, body
    payload = _assert_json_body(headers, body)
    assert isinstance(payload.get("templates"), list)
    assert payload["templates"], "expected bootstrap style templates"
    _assert_cors_ok(headers, origin)


def test_fonts_and_stroke_previews_public():
    origin = _origin()
    fonts_status, fonts_headers, fonts_body = _request("/styles/fonts", headers={"Origin": origin})
    assert fonts_status == 200, fonts_body
    fonts = _assert_json_body(fonts_headers, fonts_body)
    assert isinstance(fonts.get("fonts"), list)
    _assert_cors_ok(fonts_headers, origin)

    prev_status, prev_headers, prev_body = _request("/styles/stroke-previews", headers={"Origin": origin})
    assert prev_status == 200, prev_body
    _assert_json_body(prev_headers, prev_body)
    _assert_cors_ok(prev_headers, origin)


def test_cors_preflight_templates():
    origin = _origin()
    status, headers, _ = _request(
        "/styles/templates",
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert status in {200, 204}, f"preflight status {status}"
    _assert_cors_ok(headers, origin)


def test_protected_route_without_key_is_401():
    status, headers, body = _request(
        "/styles/templates",
        method="POST",
        headers={"Origin": _origin(), "Content-Type": "application/json"},
        data=b"{}",
    )
    assert status == 401, body
    payload = _assert_json_body(headers, body)
    assert "API key" in payload.get("detail", "")
    _assert_cors_ok(headers, _origin())


def test_presign_without_key_is_401():
    status, headers, body = _request(
        "/jobs/presign",
        method="POST",
        headers={"Origin": _origin(), "Content-Type": "application/json"},
        data=b'{"filename":"clip.mp4","content_type":"video/mp4"}',
    )
    assert status == 401, body
    payload = _assert_json_body(headers, body)
    assert "API key" in payload.get("detail", "")
    _assert_cors_ok(headers, _origin())


def test_protected_route_with_current_secret_not_401():
    key = _api_key()
    assert key, "API key from Secrets Manager was empty"
    status, headers, body = _request(
        "/styles/templates",
        method="POST",
        headers={
            "Origin": _origin(),
            "Content-Type": "application/json",
            "x-api-key": key,
        },
        data=b"{}",
    )
    assert status != 401, body
    # Empty/invalid payload should be 400, not auth failure
    assert status in {400, 422}, body
    _assert_cors_ok(headers, _origin())


def test_no_comma_star_cors_on_api():
    """Regression: Function URL / FastAPI must not emit '*, https://…'."""
    origin = _origin()
    for path in ("/health", "/styles/templates"):
        _, headers, _ = _request(path, headers={"Origin": origin})
        for key, value in headers.items():
            if key.lower() == "access-control-allow-origin":
                assert value.strip() not in {f"*, {origin}", f"{origin}, *"}
                assert not value.strip().startswith("*,")
                assert ", http" not in value.lower()
