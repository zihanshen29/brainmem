from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from brain.ledger.reader import read_all
from brain.mcp.formatters import to_jsonable
from brain.paths import BrainPaths
from brain.pipeline.ask import ask as _ask
from brain.pipeline.capture import capture as _capture
from brain.pipeline.review import list_pending as _list_pending
from brain.pipeline.status import collect_status as _collect_status

DEFAULT_INJECT_TOP = 8


def _root(brain_root: str | Path) -> Path:
    return Path(brain_root).expanduser()


def brain_status(brain_root: str | Path = ".") -> dict[str, Any]:
    """When to call: need a read-only local-only health summary of a BrainMem repository."""
    return to_jsonable(_collect_status(_root(brain_root)))


def brain_ask(
    query: str,
    brain_root: str | Path = ".",
    top: int = 5,
    mode: str = "keyword-only",
    page_type: str | None = None,
    explain: bool = False,
    show_sql: bool = False,
    debug: bool = False,
) -> dict[str, Any]:
    """When to call: need BrainMem recall; default keyword-only mode is local-only, while hybrid/semantic/explain may call providers."""
    return to_jsonable(
        _ask(
            _root(brain_root),
            query,
            top=top,
            page_type=page_type,
            explain=explain,
            show_sql=show_sql,
            mode=mode,
            debug=debug,
        )
    )


def brain_capture(
    text: str,
    brain_root: str | Path = ".",
    kind: str = "note",
    source: str = "stdin",
    source_ref: str | None = None,
) -> dict[str, Any]:
    """When to call: need to write raw text to durable laundry for later ingest; this does not ingest or call providers."""
    return to_jsonable(
        _capture(
            _root(brain_root),
            text,
            kind=kind,
            source=source,
            source_ref=source_ref,
            auto_commit=False,
        )
    )


def brain_inject(
    query: str,
    brain_root: str | Path = ".",
    budget: int = 10000,
    output_format: str = "markdown",
    mode: str = "keyword-only",
    top: int = DEFAULT_INJECT_TOP,
    include_snapshot: bool = True,
) -> dict[str, Any]:
    """When to call: need an injection-ready context pack; default keyword-only mode avoids providers unless another mode is requested. The snapshot is a local file fragment from scratch/SNAPSHOT.md."""
    injection = import_module("brain.pipeline.injection")
    return to_jsonable(
        injection.inject(
            _root(brain_root),
            query,
            budget=budget,
            output_format=output_format,
            mode=mode,
            top=top,
            include_snapshot=include_snapshot,
        )
    )


def brain_review_queue(
    brain_root: str | Path = ".",
    kind: str | None = None,
) -> dict[str, Any]:
    """When to call: need to list pending review items locally; this never approves, rejects, or applies decisions."""
    items = _list_pending(_root(brain_root), kind=kind)
    return {"items": to_jsonable(items), "count": len(items)}


def brain_recent_events(
    brain_root: str | Path = ".",
    limit: int = 10,
    kind: str | None = None,
) -> dict[str, Any]:
    """When to call: need the newest append-only ledger events for local-only context."""
    if limit < 1:
        raise ValueError("limit must be positive")

    paths = BrainPaths(_root(brain_root))
    events = list(read_all(paths.events_jsonl))
    if kind is not None:
        events = [event for event in events if event.kind.value == kind]
    recent = list(reversed(events))[:limit]
    return {"events": to_jsonable(recent), "count": len(recent)}
