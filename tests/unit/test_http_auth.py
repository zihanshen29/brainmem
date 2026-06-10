from __future__ import annotations

import asyncio

import pytest

from brain.mcp.http_auth import (
    TOKEN_HEADER,
    TokenAuthMiddleware,
    is_token_authorized,
    require_token,
    token_from_env,
)


def test_token_from_env_returns_none_when_unconfigured() -> None:
    assert token_from_env(None, {"BRAINMEM_TOKEN": "secret"}) is None
    assert token_from_env("BRAINMEM_TOKEN", {}) is None
    assert token_from_env("BRAINMEM_TOKEN", {"BRAINMEM_TOKEN": ""}) is None


def test_token_from_env_reads_configured_env_name() -> None:
    assert token_from_env("BRAINMEM_TOKEN", {"BRAINMEM_TOKEN": "secret"}) == "secret"
    assert token_from_env("CUSTOM_TOKEN", {"CUSTOM_TOKEN": "secret"}) == "secret"


def test_missing_configured_token_allows_request() -> None:
    assert is_token_authorized({}, None) is True
    assert is_token_authorized({}, "") is True


def test_missing_request_token_is_rejected() -> None:
    assert is_token_authorized({}, "secret") is False


def test_wrong_request_token_is_rejected() -> None:
    assert is_token_authorized({TOKEN_HEADER: "wrong"}, "secret") is False


def test_correct_request_token_is_accepted() -> None:
    assert is_token_authorized({TOKEN_HEADER: "secret"}, "secret") is True


def test_header_name_is_case_insensitive() -> None:
    assert is_token_authorized({"x-brainmem-token": "secret"}, "secret") is True


def test_require_token_raises_permission_error_for_invalid_token() -> None:
    with pytest.raises(PermissionError, match="invalid BrainMem token"):
        require_token({TOKEN_HEADER: "wrong"}, "secret")


def test_token_auth_middleware_allows_valid_request() -> None:
    calls: list[str] = []
    messages: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        calls.append("app")

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    middleware = TokenAuthMiddleware(app, "secret")
    asyncio.run(
        middleware(
            {"type": "http", "headers": [(b"x-brainmem-token", b"secret")]},
            object(),
            send,
        )
    )

    assert calls == ["app"]
    assert messages == []


def test_token_auth_middleware_rejects_invalid_request() -> None:
    calls: list[str] = []
    messages: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        calls.append("app")

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    middleware = TokenAuthMiddleware(app, "secret")
    asyncio.run(middleware({"type": "http", "headers": []}, object(), send))

    assert calls == []
    assert messages[0]["status"] == 401


def test_token_auth_middleware_passes_lifespan_scope_to_app() -> None:
    calls: list[object] = []
    messages: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        calls.append(scope)

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {"type": "lifespan"}
    middleware = TokenAuthMiddleware(app, "secret")
    asyncio.run(middleware(scope, object(), send))

    assert calls == [scope]
    assert messages == []
