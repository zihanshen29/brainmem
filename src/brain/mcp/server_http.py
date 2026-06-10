from __future__ import annotations

import inspect
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from brain.exceptions import BrainError
from brain.mcp import tools
from brain.mcp.http_auth import TokenAuthMiddleware, token_from_env
from brain.mcp.http_config import HttpConfig, parse_args
from brain.mcp.server import TOOL_NAMES

LOGGER = logging.getLogger(__name__)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/](?:[^\s:;,\]\[(){}<>\"']+[\\/]?)+|\\\\[^\s:;,\]\[(){}<>\"']+)"
)
_UNIX_PATH_RE = re.compile(r"(?<![\w])/(?:[^\s:;,\]\[(){}<>\"']+/)*[^\s:;,\]\[(){}<>\"']+")


def build_server(config: HttpConfig) -> Any:
    """Build the BrainMem HTTP/SSE MCP server with a fixed brain root."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "The 'mcp' package is required to start the BrainMem HTTP MCP server. "
            "Install project dependencies or run tests against brain.mcp.tools."
        ) from exc

    server = FastMCP(
        "brainmem",
        host=config.host,
        port=config.port,
        log_level=config.log_level.upper(),
    )
    for name in TOOL_NAMES:
        if name not in config.enabled_tools:
            continue
        tool: Callable[..., Any] = getattr(tools, name)
        server.tool()(_fixed_brain_root_tool(tool, config.brain_root))
    return server


def build_sse_app(
    config: HttpConfig,
    env: Mapping[str, str] | None = None,
) -> TokenAuthMiddleware:
    """Build an authenticated ASGI app for the FastMCP SSE transport."""
    token = token_from_env(config.token_env, env)
    if not token:
        LOGGER.warning(
            "BrainMem HTTP token is not configured; requests will be unauthenticated."
        )
    return TokenAuthMiddleware(build_server(config).sse_app(), token)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the BrainMem HTTP/SSE MCP server."""
    import sys

    import uvicorn

    config = parse_args(sys.argv[1:] if argv is None else argv)
    warn_if_possible_concurrent_writes(config.brain_root)
    uvicorn.run(
        build_sse_app(config),
        host=config.host,
        port=config.port,
        log_level=config.log_level,
    )


def _fixed_brain_root_tool(
    tool: Callable[..., Any],
    brain_root: Path,
) -> Callable[..., Any]:
    signature = inspect.signature(tool)
    exposed_parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.name != "brain_root"
    ]
    exposed_signature = signature.replace(
        parameters=exposed_parameters,
        return_annotation=dict[str, Any],
    )

    @wraps(tool)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("brain_root", None)
        try:
            return tool(*args, brain_root=brain_root, **kwargs)
        except BrainError as exc:
            return {
                "error": {
                    "code": "brain_error",
                    "message": sanitize_error_text(str(exc)),
                }
            }
        except ValidationError as exc:
            return {
                "error": {
                    "code": "validation_error",
                    "details": _validation_error_details(exc),
                }
            }
        except Exception:
            LOGGER.exception("Unhandled BrainMem HTTP tool error")
            return {"error": {"code": "internal", "message": "internal error"}}

    wrapper.__signature__ = exposed_signature  # type: ignore[attr-defined]
    if hasattr(wrapper, "__annotations__"):
        wrapper.__annotations__ = {
            key: value
            for key, value in wrapper.__annotations__.items()
            if key != "brain_root"
        }
        wrapper.__annotations__["return"] = dict[str, Any]
    return wrapper


def warn_if_possible_concurrent_writes(brain_root: Path) -> None:
    """Warn about possible concurrent SQLite writers without blocking startup."""
    db_path = brain_root / "brain.db"
    wal_path = brain_root / "brain.db-wal"

    if wal_path.exists():
        LOGGER.warning(
            "BrainMem HTTP server detected brain.db-wal; another writer may be active."
        )

    if not db_path.exists():
        return

    try:
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=0.1) as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.Error:
        LOGGER.warning(
            "BrainMem HTTP server could not inspect brain.db; another writer may be active."
        )
        return

    if journal_mode and str(journal_mode[0]).lower() == "wal" and wal_path.exists():
        LOGGER.warning(
            "BrainMem HTTP server detected WAL mode with a WAL file; concurrent writes may occur."
        )


def sanitize_error_text(text: str) -> str:
    clean = _WINDOWS_PATH_RE.sub("<path>", text)
    return _UNIX_PATH_RE.sub("<path>", clean)


def _validation_error_details(exc: ValidationError) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = error.get("loc", ())
        field = ".".join(str(part) for part in loc) if isinstance(loc, tuple) else str(loc)
        details.append(
            {
                "field": sanitize_error_text(field),
                "message": sanitize_error_text(str(error.get("msg", "invalid value"))),
            }
        )
    return details


if __name__ == "__main__":
    main()
