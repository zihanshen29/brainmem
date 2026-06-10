from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from brain.exceptions import ConfigError

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_TOKEN_ENV = "BRAINMEM_TOKEN"
DEFAULT_LOG_LEVEL = "info"

ALL_REMOTE_TOOLS = frozenset(
    {
        "brain_status",
        "brain_ask",
        "brain_capture",
        "brain_inject",
        "brain_scratch_append",
        "brain_snapshot_rebuild",
        "brain_procedure_list",
        "brain_procedure_new",
        "brain_procedure_run",
        "brain_procedure_promote",
        "brain_review_queue",
        "brain_recent_events",
    }
)

OPT_IN_REMOTE_TOOLS = frozenset(
    {
        "brain_procedure_new",
        "brain_procedure_promote",
    }
)

DEFAULT_REMOTE_TOOLS = frozenset(
    {
        "brain_status",
        "brain_ask",
        "brain_capture",
        "brain_inject",
        "brain_scratch_append",
        "brain_snapshot_rebuild",
        "brain_procedure_list",
        "brain_procedure_run",
        "brain_review_queue",
        "brain_recent_events",
    }
)

_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
_INITIAL_ROOT_DIRS = ("pages", "raw", "laundry", "review")


@dataclass(frozen=True)
class HttpConfig:
    brain_root: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token_env: str = DEFAULT_TOKEN_ENV
    enabled_tools: frozenset[str] = DEFAULT_REMOTE_TOOLS
    log_level: str = DEFAULT_LOG_LEVEL


def parse_args(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> HttpConfig:
    """Parse HTTP server configuration without starting a server."""
    values = env if env is not None else os.environ
    parser = argparse.ArgumentParser(prog="mem-mcp-http")
    parser.add_argument("--brain-root", default=values.get("BRAIN_ROOT"))
    parser.add_argument(
        "--host",
        default=_env_value(values, "BRAINMEM_HOST", "BRAINMEM_HTTP_HOST", DEFAULT_HOST),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int(values, "BRAINMEM_PORT", "BRAINMEM_HTTP_PORT", DEFAULT_PORT),
    )
    parser.add_argument(
        "--token-env",
        default=_env_value(
            values,
            "BRAINMEM_TOKEN_ENV",
            "BRAINMEM_HTTP_TOKEN_ENV",
            DEFAULT_TOKEN_ENV,
        ),
    )
    parser.add_argument("--enable-tool", action="append", default=[])
    parser.add_argument("--disable-tool", action="append", default=[])
    parser.add_argument(
        "--log-level",
        default=_env_value(
            values,
            "BRAINMEM_LOG_LEVEL",
            "BRAINMEM_HTTP_LOG_LEVEL",
            DEFAULT_LOG_LEVEL,
        ),
    )

    try:
        namespace = parser.parse_args(list(argv or []))
    except argparse.ArgumentError as exc:
        raise ConfigError("invalid HTTP configuration arguments") from exc
    except SystemExit as exc:
        if exc.code == 0:
            raise
        raise ConfigError("invalid HTTP configuration arguments") from exc

    brain_root_arg = namespace.brain_root
    if not brain_root_arg:
        raise ConfigError("brain root is required")

    brain_root = validate_brain_root(Path(brain_root_arg).expanduser())
    enabled_tools = resolve_enabled_tools(
        enable=_split_tool_args(namespace.enable_tool),
        disable=_split_tool_args(namespace.disable_tool),
    )
    log_level = str(namespace.log_level).lower()
    if log_level not in _LOG_LEVELS:
        raise ConfigError("invalid log level")

    if namespace.port < 1 or namespace.port > 65535:
        raise ConfigError("port must be between 1 and 65535")

    return HttpConfig(
        brain_root=brain_root,
        host=str(namespace.host),
        port=namespace.port,
        token_env=str(namespace.token_env),
        enabled_tools=enabled_tools,
        log_level=log_level,
    )


def resolve_enabled_tools(
    *,
    enable: Sequence[str] = (),
    disable: Sequence[str] = (),
) -> frozenset[str]:
    unknown = sorted((set(enable) | set(disable)) - ALL_REMOTE_TOOLS)
    if unknown:
        raise ConfigError(f"unknown remote tool: {unknown[0]}")

    tools = set(DEFAULT_REMOTE_TOOLS)
    tools.update(enable)
    tools.difference_update(disable)
    return frozenset(tools)


def validate_brain_root(root: Path) -> Path:
    if not root.exists():
        raise ConfigError("brain root does not exist")
    if not root.is_dir():
        raise ConfigError("brain root must be a directory")
    if not os.access(root, os.R_OK | os.W_OK):
        raise ConfigError("brain root is not readable and writable")
    if not _looks_like_brain_root(root):
        raise ConfigError("brain root does not look initialized")
    return root


def _looks_like_brain_root(root: Path) -> bool:
    events_jsonl = root / "events.jsonl"
    if events_jsonl.is_file():
        return True
    return all((root / name).is_dir() for name in _INITIAL_ROOT_DIRS)


def _split_tool_args(values: Sequence[str]) -> list[str]:
    tools: list[str] = []
    for value in values:
        tools.extend(part.strip() for part in value.split(",") if part.strip())
    return tools


def _env_value(
    env: Mapping[str, str],
    primary: str,
    fallback: str,
    default: str,
) -> str:
    value = env.get(primary)
    if value is not None and value != "":
        return value
    fallback_value = env.get(fallback)
    if fallback_value is not None and fallback_value != "":
        return fallback_value
    return default


def _env_int(env: Mapping[str, str], primary: str, fallback: str, default: int) -> int:
    value = env.get(primary)
    name = primary
    if value is None or value == "":
        value = env.get(fallback)
        name = fallback
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
