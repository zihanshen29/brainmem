from __future__ import annotations

import hmac
import ipaddress
import os
from collections.abc import Awaitable, Callable, Mapping

AsgiApp = Callable[[object, object, object], Awaitable[None]]
AsgiSend = Callable[[dict[str, object]], Awaitable[None]]

TOKEN_HEADER = "X-Brainmem-Token"


def token_from_env(
    token_env: str | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    if not token_env:
        return None
    values = env if env is not None else os.environ
    token = values.get(token_env)
    if token is None or token.strip() == "":
        return None
    return token


def is_token_authorized(
    headers: Mapping[str, str],
    expected_token: str | None,
) -> bool:
    """Return True when the request is authorized by the configured token."""
    if not expected_token:
        return False

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

    def __init__(
        self,
        app: AsgiApp,
        expected_token: str | None,
        *,
        allow_unauthenticated: bool = False,
    ) -> None:
        self.app = app
        self.expected_token = expected_token
        self.allow_unauthenticated = allow_unauthenticated

    async def __call__(self, scope: object, receive: object, send: AsgiSend) -> None:
        if not isinstance(scope, Mapping) or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if not self.expected_token:
            if self.allow_unauthenticated:
                await self.app(scope, receive, send)
                return
            await _send_unauthorized(send)
            return

        headers = _headers_from_scope(scope)
        if is_token_authorized(headers, self.expected_token):
            await self.app(scope, receive, send)
            return

        await _send_unauthorized(send)


def is_loopback_host(host: str) -> bool:
    """Return whether a bind host is unambiguously local-only."""
    normalized = host.strip()
    if normalized.casefold() == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]

    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False

    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


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


async def _send_unauthorized(send: AsgiSend) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": b"unauthorized"})
