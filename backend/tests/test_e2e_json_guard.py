"""Guards that Cloud E2E rejects SPA HTML masquerading as API 200."""
from __future__ import annotations

import importlib
import os
import sys

os.environ.setdefault("E2E_BASE_URL", "https://example.invalid/api")

E2E = os.path.abspath(os.path.join(os.path.dirname(__file__), "e2e"))
if E2E not in sys.path:
    sys.path.insert(0, E2E)

test_cloud = importlib.import_module("test_cloud")


def test_json_guard_accepts_json():
    payload = test_cloud._assert_json_body(
        {"Content-Type": "application/json; charset=utf-8"},
        b'{"status":"ok"}',
    )
    assert payload["status"] == "ok"


def test_json_guard_rejects_spa_html():
    html = b"<!DOCTYPE html><html lang=\"en\"><head><title>Autocaption</title></head></html>"
    try:
        test_cloud._assert_json_body({"Content-Type": "text/html"}, html)
    except AssertionError as exc:
        assert "application/json" in str(exc)
        return
    raise AssertionError("HTML body must not pass the JSON guard")
