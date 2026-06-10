from __future__ import annotations

from pathlib import Path

import pytest

from brain.exceptions import ConfigError
from brain.mcp.http_config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_REMOTE_TOOLS,
    DEFAULT_TOKEN_ENV,
    OPT_IN_REMOTE_TOOLS,
    parse_args,
    resolve_enabled_tools,
    validate_brain_root,
)


def initialized_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    root.mkdir()
    (root / "events.jsonl").write_text("", encoding="utf-8")
    return root


def initial_structure_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    root.mkdir()
    for name in ("pages", "raw", "laundry", "review"):
        (root / name).mkdir()
    return root


def test_parse_args_defaults_host_port_and_tools(tmp_path: Path) -> None:
    root = initialized_root(tmp_path)

    config = parse_args(["--brain-root", str(root)], env={})

    assert config.brain_root == root
    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT
    assert config.token_env == DEFAULT_TOKEN_ENV
    assert config.enabled_tools == DEFAULT_REMOTE_TOOLS
    assert "brain_review_apply" not in config.enabled_tools
    assert config.enabled_tools.isdisjoint(OPT_IN_REMOTE_TOOLS)


def test_parse_args_accepts_env_defaults(tmp_path: Path) -> None:
    root = initialized_root(tmp_path)

    config = parse_args(
        [],
        env={
            "BRAIN_ROOT": str(root),
            "BRAINMEM_HOST": "127.0.0.1",
            "BRAINMEM_PORT": "9000",
            "BRAINMEM_TOKEN_ENV": "CUSTOM_TOKEN",
            "BRAINMEM_LOG_LEVEL": "debug",
        },
    )

    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert config.token_env == "CUSTOM_TOKEN"
    assert config.log_level == "debug"


def test_parse_args_prefers_primary_env_over_legacy_http_fallback(tmp_path: Path) -> None:
    root = initialized_root(tmp_path)

    config = parse_args(
        [],
        env={
            "BRAIN_ROOT": str(root),
            "BRAINMEM_HOST": "127.0.0.1",
            "BRAINMEM_HTTP_HOST": "legacy-host",
            "BRAINMEM_PORT": "9000",
            "BRAINMEM_HTTP_PORT": "8000",
            "BRAINMEM_TOKEN_ENV": "PRIMARY_TOKEN",
            "BRAINMEM_HTTP_TOKEN_ENV": "LEGACY_TOKEN",
            "BRAINMEM_LOG_LEVEL": "debug",
            "BRAINMEM_HTTP_LOG_LEVEL": "warning",
        },
    )

    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert config.token_env == "PRIMARY_TOKEN"
    assert config.log_level == "debug"


def test_parse_args_accepts_legacy_http_env_as_fallback(tmp_path: Path) -> None:
    root = initialized_root(tmp_path)

    config = parse_args(
        [],
        env={
            "BRAIN_ROOT": str(root),
            "BRAINMEM_HTTP_HOST": "127.0.0.1",
            "BRAINMEM_HTTP_PORT": "9000",
            "BRAINMEM_HTTP_TOKEN_ENV": "LEGACY_TOKEN",
            "BRAINMEM_HTTP_LOG_LEVEL": "warning",
        },
    )

    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert config.token_env == "LEGACY_TOKEN"
    assert config.log_level == "warning"


def test_parse_args_help_exits_normally() -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"], env={})

    assert exc_info.value.code == 0


def test_validate_brain_root_rejects_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ConfigError, match="brain root does not exist") as exc_info:
        validate_brain_root(missing)

    assert str(missing) not in str(exc_info.value)


def test_validate_brain_root_accepts_initial_structure(tmp_path: Path) -> None:
    root = initial_structure_root(tmp_path)

    assert validate_brain_root(root) == root


def test_validate_brain_root_rejects_uninitialized_directory(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    with pytest.raises(ConfigError, match="does not look initialized"):
        validate_brain_root(root)


def test_resolve_enabled_tools_can_opt_in_procedure_tools() -> None:
    tools = resolve_enabled_tools(enable=["brain_procedure_new"])

    assert "brain_procedure_new" in tools
    assert "brain_procedure_promote" not in tools


def test_resolve_enabled_tools_can_disable_default_tool() -> None:
    tools = resolve_enabled_tools(disable=["brain_capture"])

    assert "brain_capture" not in tools
    assert "brain_status" in tools


def test_resolve_enabled_tools_enable_then_disable_wins() -> None:
    tools = resolve_enabled_tools(
        enable=["brain_procedure_promote"],
        disable=["brain_procedure_promote"],
    )

    assert "brain_procedure_promote" not in tools


def test_resolve_enabled_tools_rejects_unknown_tool() -> None:
    with pytest.raises(ConfigError, match="unknown remote tool: brain_review_apply"):
        resolve_enabled_tools(enable=["brain_review_apply"])


def test_parse_args_splits_repeated_and_comma_tool_flags(tmp_path: Path) -> None:
    root = initialized_root(tmp_path)

    config = parse_args(
        [
            "--brain-root",
            str(root),
            "--enable-tool",
            "brain_procedure_new,brain_procedure_promote",
            "--disable-tool",
            "brain_capture",
        ],
        env={},
    )

    assert "brain_procedure_new" in config.enabled_tools
    assert "brain_procedure_promote" in config.enabled_tools
    assert "brain_capture" not in config.enabled_tools
