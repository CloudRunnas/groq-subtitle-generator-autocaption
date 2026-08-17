"""API key auth for backend (X-API-Key or Authorization: Bearer)."""
from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


PUBLIC_PATHS = {
    "/",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
}

# Read-only style catalog (templates, fonts, stroke previews). Mutations stay protected.
_PUBLIC_GET_PREFIXES = (
    "/styles/templates",
    "/styles/fonts",
    "/styles/stroke-previews",
)


def is_public_request(method: str, path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/styles/stroke-previews/"):
        return True
    if (method or "").upper() in ("GET", "HEAD", "OPTIONS"):
        for prefix in _PUBLIC_GET_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                return True
    return False


def extract_api_key(headers: dict[str, str], query_api_key: str = "") -> str:
    """headers keys should be lower-case."""
    provided = headers.get("x-api-key") or ""
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    if not provided:
        provided = (query_api_key or "").strip()
    return provided


def api_key_accepted(provided: str, expected: str) -> bool:
    if not expected:
        return True
    if not provided:
        return False
    if len(provided) != len(expected):
        return False
    return secrets.compare_digest(provided, expected)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_key: str):
        super().__init__(app)
        self.api_key = (api_key or "").strip()

    async def dispatch(self, request, call_next):
        if not self.api_key:
            return await call_next(request)

        path = request.url.path
        if request.method == "OPTIONS" or is_public_request(request.method, path):
            return await call_next(request)

        provided = extract_api_key(
            {
                "x-api-key": request.headers.get("x-api-key") or "",
                "authorization": request.headers.get("authorization") or "",
            },
            request.query_params.get("api_key") or "",
        )

        if not api_key_accepted(provided, self.api_key):
            return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)

        return await call_next(request)
