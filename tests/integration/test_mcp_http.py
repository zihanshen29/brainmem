from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from brain.exceptions import BrainError
from brain.mcp import server_http, tools
from brain.mcp.http_auth import TokenAuthMiddleware
from brain.mcp.http_config import DEFAULT_REMOTE_TOOLS, HttpConfig
from brain.mcp.server_http import (
    build_server,
    build_sse_app,
    warn_if_possible_concurrent_writes,
)


def test_build_server_registers_default_remote_whitelist(brain_root: Path) -> None:
    server = build_server(HttpConfig(brain_root=brain_root))

    registered = set(server._tool_manager._tools)

    assert registered == DEFAULT_REMOTE_TOOLS
    assert "brain_procedure_new" not in registered
    assert "brain_procedure_promote" not in registered


def test_build_server_registers_opt_in_tool(brain_root: Path) -> None:
    server = build_server(
        HttpConfig(
            brain_root=brain_root,
            enabled_tools=frozenset({"brain_status", "brain_procedure_new"}),
        )
    )

    assert set(server._tool_manager._tools) == {"brain_status", "brain_procedure_new"}


def test_registered_tool_signature_hides_brain_root(brain_root: Path) -> None:
    server = build_server(
        HttpConfig(brain_root=brain_root, enabled_tools=frozenset({"brain_ask"}))
    )
    registered = server._tool_manager.get_tool("brain_ask")

    assert "brain_root" not in registered.parameters["properties"]


def test_registered_tool_uses_fixed_brain_root(
    monkeypatch: pytest.MonkeyPatch,
    brain_root: Path,
    tmp_path: Path,
) -> None:
    calls: list[Path] = []

    def fake_status(root: Path) -> dict[str, str]:
        calls.append(root)
        return {"brain_root": str(root)}

    monkeypatch.setattr(tools, "_collect_status", fake_status)
    server = build_server(
        HttpConfig(brain_root=brain_root, enabled_tools=frozenset({"brain_status"}))
    )

    result = asyncio.run(
        server.call_tool("brain_status", {"brain_root": str(tmp_path / "client-root")})
    )

    assert calls == [brain_root]
    assert json.loads(result[0][0].text) == {"brain_root": str(brain_root)}


def test_brain_error_returns_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
    brain_root: Path,
) -> None:
    def fake_status(brain_root: Path = Path(".")) -> dict[str, str]:
        raise BrainError(f"Required brain file not found: {brain_root / 'config.toml'}")

    fake_status.__name__ = "brain_status"
    monkeypatch.setattr(tools, "brain_status", fake_status)
    server = build_server(
        HttpConfig(brain_root=brain_root, enabled_tools=frozenset({"brain_status"}))
    )

    result = asyncio.run(server.call_tool("brain_status", {}))
    payload = json.loads(result[0][0].text)

    assert payload == {
        "error": {
            "code": "brain_error",
            "message": "Required brain file not found: <path>",
        }
    }
    assert str(brain_root) not in result[0][0].text


def test_validation_error_returns_field_and_sanitized_reason(
    monkeypatch: pytest.MonkeyPatch,
    brain_root: Path,
) -> None:
    class Request(BaseModel):
        count: int = Field(gt=0)

    def fake_status(brain_root: Path = Path(".")) -> dict[str, str]:
        Request.model_validate({"count": 0, "path": str(brain_root / "secret.md")})
        return {"ok": "true"}

    fake_status.__name__ = "brain_status"
    monkeypatch.setattr(tools, "brain_status", fake_status)
    server = build_server(
        HttpConfig(brain_root=brain_root, enabled_tools=frozenset({"brain_status"}))
    )

    result = asyncio.run(server.call_tool("brain_status", {}))
    payload = json.loads(result[0][0].text)

    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"] == [
        {"field": "count", "message": "Input should be greater than 0"}
    ]
    assert str(brain_root) not in result[0][0].text


def test_unknown_error_logs_stack_and_returns_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    brain_root: Path,
) -> None:
    def fake_status(brain_root: Path = Path(".")) -> dict[str, str]:
        raise RuntimeError(f"boom at {brain_root / 'secret.md'}")

    fake_status.__name__ = "brain_status"
    monkeypatch.setattr(tools, "brain_status", fake_status)
    server = build_server(
        HttpConfig(brain_root=brain_root, enabled_tools=frozenset({"brain_status"}))
    )

    with caplog.at_level("ERROR"):
        result = asyncio.run(server.call_tool("brain_status", {}))

    assert json.loads(result[0][0].text) == {
        "error": {"code": "internal", "message": "internal error"}
    }
    assert "Unhandled BrainMem HTTP tool error" in caplog.text
    assert "RuntimeError" in caplog.text
    assert str(brain_root) not in result[0][0].text


def test_build_sse_app_warns_when_token_missing(
    caplog: pytest.LogCaptureFixture,
    brain_root: Path,
) -> None:
    config = HttpConfig(brain_root=brain_root, token_env="BRAINMEM_TEST_TOKEN")

    with caplog.at_level("WARNING"):
        app = build_sse_app(config, env={})

    assert isinstance(app, TokenAuthMiddleware)
    assert app.expected_token is None
    assert "token is not configured" in caplog.text


def test_build_sse_app_wraps_token_auth_when_token_set(brain_root: Path) -> None:
    app = build_sse_app(
        HttpConfig(brain_root=brain_root, token_env="BRAINMEM_TEST_TOKEN"),
        env={"BRAINMEM_TEST_TOKEN": "secret"},
    )

    assert isinstance(app, TokenAuthMiddleware)
    assert app.expected_token == "secret"


def test_mem_mcp_http_entry_point_exists() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["mem-mcp-http"] == "brain.mcp.server_http:main"


def test_warn_if_possible_concurrent_writes_detects_wal_file(
    caplog: pytest.LogCaptureFixture,
    brain_root: Path,
) -> None:
    (brain_root / "brain.db-wal").write_text("", encoding="utf-8")

    with caplog.at_level("WARNING"):
        warn_if_possible_concurrent_writes(brain_root)

    assert "brain.db-wal" in caplog.text


def test_main_checks_concurrent_writes_before_start(
    monkeypatch: pytest.MonkeyPatch,
    brain_root: Path,
) -> None:
    calls: list[object] = []
    config = HttpConfig(brain_root=brain_root)

    monkeypatch.setattr(server_http, "parse_args", lambda argv: config)
    monkeypatch.setattr(
        server_http,
        "warn_if_possible_concurrent_writes",
        lambda root: calls.append(("warn", root)),
    )
    monkeypatch.setattr(server_http, "build_sse_app", lambda app_config: "app")

    def fake_run(app: object, **kwargs: object) -> None:
        calls.append(("run", app, kwargs))

    monkeypatch.setattr("uvicorn.run", fake_run)

    server_http.main(["--brain-root", str(brain_root)])

    assert calls[0] == ("warn", brain_root)
    assert calls[1][0:2] == ("run", "app")
