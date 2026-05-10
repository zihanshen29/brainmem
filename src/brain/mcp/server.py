from __future__ import annotations

from collections.abc import Callable
from typing import Any

from brain.mcp import tools

TOOL_NAMES = [
    "brain_status",
    "brain_ask",
    "brain_capture",
    "brain_inject",
    "brain_review_queue",
    "brain_recent_events",
]


def build_server() -> Any:
    """Build the BrainMem MCP server."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "The 'mcp' package is required to start the BrainMem MCP server. "
            "Install project dependencies or run tests against brain.mcp.tools."
        ) from exc

    server = FastMCP("brainmem")
    for name in TOOL_NAMES:
        tool: Callable[..., Any] = getattr(tools, name)
        server.tool()(tool)
    return server


def main() -> None:
    """Run the BrainMem MCP server."""
    build_server().run()


if __name__ == "__main__":
    main()
