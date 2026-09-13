from __future__ import annotations

from collections.abc import Callable
from typing import Any

from brain.db.connection import prepare_sqlite_extension
from brain.mcp import tools
from brain.mcp.dispatch import READ_TOOLS, ToolDispatcher

TOOL_NAMES = [
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
]


def build_server(*, dispatcher: ToolDispatcher | None = None) -> Any:
    """Build the BrainMem MCP server."""
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError(
            "The 'mcp' package is required to start the BrainMem MCP server. "
            "Install project dependencies or run tests against brain.mcp.tools."
        ) from exc

    prepare_sqlite_extension()
    dispatcher = dispatcher or ToolDispatcher()
    server = FastMCP("brainmem", lifespan=dispatcher.lifespan)
    for name in TOOL_NAMES:
        tool: Callable[..., Any] = getattr(tools, name)
        server.tool(annotations=ToolAnnotations(readOnlyHint=name in READ_TOOLS))(
            dispatcher.wrap(tool)
        )
    return server


def main() -> None:
    """Run the BrainMem MCP server."""
    build_server().run()


if __name__ == "__main__":
    main()
