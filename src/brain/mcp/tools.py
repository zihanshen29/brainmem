from __future__ import annotations

import re
from importlib import import_module
from pathlib import Path
from typing import Any

from brain.ledger.reader import read_all
from brain.mcp.formatters import to_jsonable
from brain.models import PageType
from brain.pages import parse_page
from brain.paths import BrainPaths
from brain.pipeline.ask import ask as _ask
from brain.pipeline.capture import capture as _capture
from brain.pipeline.review import list_pending as _list_pending
from brain.pipeline.status import collect_status as _collect_status

DEFAULT_INJECT_TOP = 8
SOURCE_AGENT_RE = re.compile(r"^source_agent:\s*\S+", re.MULTILINE)
SOURCE_CONTEXT_RE = re.compile(r"^source_context:\s*\S+", re.MULTILINE)


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
    """When to call: need BrainMem recall; default keyword-only mode is local-only, while hybrid/semantic/explain may call providers. Return warnings when retrieval downgrades."""
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
    *,
    source_agent: str,
    brain_root: str | Path = ".",
    kind: str = "note",
    source: str = "stdin",
    source_ref: str | None = None,
    source_context: str | None = None,
) -> dict[str, Any]:
    """When to call: need to write raw text to durable laundry for later ingest; this does not ingest or call providers. Pass the caller identity as source_agent."""
    structured_text = _with_source_context(
        text,
        source=source,
        source_ref=source_ref,
        source_agent=source_agent,
        source_context=source_context,
    )
    return to_jsonable(
        _capture(
            _root(brain_root),
            structured_text,
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
    page_type: str | None = None,
    include_slug: list[str] | None = None,
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
            page_type=page_type,
            include_slugs=include_slug or [],
        )
    )


def brain_scratch_append(
    text: str,
    brain_root: str | Path = ".",
    source: str = "mcp",
) -> dict[str, Any]:
    """When to call: need to record local session-progress notes in scratch/working.md without promoting them to wiki truth."""
    scratch = import_module("brain.pipeline.scratch")
    return to_jsonable(
        scratch.append_working(
            _root(brain_root),
            text,
            source=source,
        )
    )


def brain_snapshot_rebuild(
    brain_root: str | Path = ".",
    max_items: int = 20,
    max_chars: int = 8000,
    strategy: str = "dedup",
) -> dict[str, Any]:
    """When to call: need to rebuild the local scratch/SNAPSHOT.md current-state fragment before injection; dedup is the default local strategy."""
    scratch = import_module("brain.pipeline.scratch")
    return to_jsonable(
        scratch.rebuild_snapshot(
            _root(brain_root),
            max_items=max_items,
            max_chars=max_chars,
            strategy=strategy,
        )
    )


def brain_procedure_list(
    brain_root: str | Path = ".",
    status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """When to call: need to discover reusable local procedure capsules before high-risk or repeatable work."""
    if limit < 1:
        raise ValueError("limit must be positive")
    paths = BrainPaths(_root(brain_root))
    rows: list[dict[str, Any]] = []
    if paths.procedures_dir.exists():
        for path in sorted(paths.procedures_dir.glob("*.md")):
            page = parse_page(path)
            if page.frontmatter.type is not PageType.PROCEDURE:
                continue
            if status is not None and getattr(page.frontmatter.status, "value", None) != status:
                continue
            rows.append(
                {
                    "slug": page.frontmatter.slug,
                    "title": page.frontmatter.title,
                    "status": page.frontmatter.status,
                    "success_count": page.frontmatter.success_count,
                    "fail_count": page.frontmatter.fail_count,
                    "last_run": page.frontmatter.last_run,
                    "path": _display_path(paths, path),
                }
            )
            if len(rows) >= limit:
                break
    return {"procedures": to_jsonable(rows), "count": len(rows)}


def brain_procedure_new(
    slug: str,
    title: str,
    brain_root: str | Path = ".",
    auto_commit: bool = False,
) -> dict[str, Any]:
    """When to call: need to create a new durable reusable procedure capsule from an explicit user or agent decision."""
    procedure = import_module("brain.pipeline.procedure")
    return to_jsonable(
        procedure.create_procedure(
            _root(brain_root),
            slug,
            title=title,
            auto_commit=auto_commit,
        )
    )


def brain_procedure_run(
    slug: str,
    result: str,
    note: str,
    brain_root: str | Path = ".",
    auto_commit: bool = False,
) -> dict[str, Any]:
    """When to call: need to record whether a reusable procedure succeeded or failed after running it."""
    procedure = import_module("brain.pipeline.procedure")
    return to_jsonable(
        procedure.run_procedure(
            _root(brain_root),
            slug,
            result=result,
            note=note,
            auto_commit=auto_commit,
        )
    )


def brain_procedure_promote(
    slug: str,
    status: str,
    brain_root: str | Path = ".",
    auto_commit: bool = False,
) -> dict[str, Any]:
    """When to call: need to manually set procedure maturity after user approval or clear evidence."""
    procedure = import_module("brain.pipeline.procedure")
    return to_jsonable(
        procedure.promote_procedure(
            _root(brain_root),
            slug,
            status=status,
            auto_commit=auto_commit,
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
    entity_slug: str | None = None,
) -> dict[str, Any]:
    """When to call: need the newest append-only ledger events for local-only context."""
    if limit < 1:
        raise ValueError("limit must be positive")

    paths = BrainPaths(_root(brain_root))
    events = list(read_all(paths.events_jsonl))
    if kind is not None:
        events = [event for event in events if event.kind.value == kind]
    if entity_slug is not None:
        events = [
            event
            for event in events
            if entity_slug in event.affected_pages
            or event.metadata.get("entity_id") == entity_slug
            or event.metadata.get("entity_slug") == entity_slug
            or event.metadata.get("procedure") == entity_slug
        ]
    recent = list(reversed(events))[:limit]
    return {"events": to_jsonable(recent), "count": len(recent)}


def _with_source_context(
    text: str,
    *,
    source: str,
    source_ref: str | None,
    source_agent: str,
    source_context: str | None,
) -> str:
    if _has_source_metadata(text):
        return text
    clean_agent = (source_agent or "").strip()
    if not clean_agent:
        raise ValueError("source_agent is required unless text already includes source context")
    clean_context = (source_context or "").strip()
    if not clean_context:
        raise ValueError("source_context is required unless text already includes source context")
    lines = [
        f"source_agent: {clean_agent}",
        f"source_context: {clean_context}",
        f"source_channel: {source}",
    ]
    if source_ref:
        lines.append(f"source_ref: {source_ref}")
    return "\n".join(lines) + "\n\n" + text


def _has_source_metadata(text: str) -> bool:
    header = text.split("\n\n", maxsplit=1)[0]
    return SOURCE_AGENT_RE.search(header) is not None and SOURCE_CONTEXT_RE.search(header) is not None


def _display_path(paths: BrainPaths, path: Path) -> str:
    try:
        return path.relative_to(paths.root).as_posix()
    except ValueError:
        return path.as_posix()
