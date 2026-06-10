from __future__ import annotations

import hmac
import os
from collections.abc import Mapping

TOKEN_HEADER = "X-Brainmem-Token"


def token_from_env(
    token_env: str | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    if not token_env:
        return None
    values = env if env is not None else os.environ
    token = values.get(token_env)
    if token is None or token == "":
        return None
    return token


def is_token_authorized(
    headers: Mapping[str, str],
    expected_token: str | None,
) -> bool:
    """Return True when the request is authorized by the configured token."""
    if not expected_token:
        return True

    provided_token = _get_header(headers, TOKEN_HEADER)
    if provided_token is None:
        return False
    return hmac.compare_digest(provided_token, expected_token)


def require_token(
    headers: Mapping[str, str],
    expected_token: str | None,
) -> None:
    if not is_token_authorized(headers, expected_token):
        raise PermissionError("invalid BrainMem token")


class TokenAuthMiddleware:
    """Small ASGI middleware compatible with Starlette-style applications."""

    def __init__(self, app: object, expected_token: str | None) -> None:
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        if not isinstance(scope, Mapping) or scope.get("type") != "http":
            await self.app(scope, receive, send)  # type: ignore[misc]
            return

        if not self.expected_token:
            await self.app(scope, receive, send)  # type: ignore[misc]
            return

        headers = _headers_from_scope(scope)
        if is_token_authorized(headers, self.expected_token):
            await self.app(scope, receive, send)  # type: ignore[misc]
            return

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"unauthorized"})


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _headers_from_scope(scope: object) -> dict[str, str]:
    if not isinstance(scope, Mapping):
        return {}
    raw_headers = scope.get("headers", [])
    headers: dict[str, str] = {}
    for key, value in raw_headers:
        headers[key.decode("latin-1")] = value.decode("latin-1")
    return headers
