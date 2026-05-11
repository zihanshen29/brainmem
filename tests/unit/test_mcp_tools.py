from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.mcp import tools
from brain.models import Frontmatter, Page, PageType, ProcedureStatus
from brain.models.event import Event, EventKind
from brain.pages import write_page

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"


@dataclass(frozen=True)
class FakeStatus:
    brain_root: Path
    pending_reviews: int


def test_mcp_tools_importable() -> None:
    from brain.mcp.tools import (
        brain_ask,
        brain_capture,
        brain_inject,
        brain_procedure_list,
        brain_procedure_new,
        brain_procedure_promote,
        brain_procedure_run,
        brain_recent_events,
        brain_review_queue,
        brain_scratch_append,
        brain_snapshot_rebuild,
        brain_status,
    )

    assert brain_status
    assert brain_ask
    assert brain_capture
    assert brain_inject
    assert brain_scratch_append
    assert brain_snapshot_rebuild
    assert brain_procedure_list
    assert brain_procedure_new
    assert brain_procedure_run
    assert brain_procedure_promote
    assert brain_review_queue
    assert brain_recent_events


def test_mcp_tool_docstrings_explain_when_to_call() -> None:
    tool_functions = [
        tools.brain_status,
        tools.brain_ask,
        tools.brain_capture,
        tools.brain_inject,
        tools.brain_scratch_append,
        tools.brain_snapshot_rebuild,
        tools.brain_procedure_list,
        tools.brain_procedure_new,
        tools.brain_procedure_run,
        tools.brain_procedure_promote,
        tools.brain_review_queue,
        tools.brain_recent_events,
    ]

    for tool_function in tool_functions:
        assert tool_function.__doc__ is not None
        assert "When to call:" in tool_function.__doc__


def test_brain_status_uses_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[Path] = []

    def fake_collect_status(root: Path) -> FakeStatus:
        calls.append(root)
        return FakeStatus(brain_root=root, pending_reviews=2)

    monkeypatch.setattr(tools, "_collect_status", fake_collect_status)

    result = tools.brain_status(tmp_path)

    assert calls == [tmp_path]
    assert result == {"brain_root": str(tmp_path), "pending_reviews": 2}


def test_brain_ask_defaults_to_keyword_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_ask(root: Path, query: str, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, "query": query, **kwargs})
        return {"query": query, "mode": kwargs["mode"], "results": []}

    monkeypatch.setattr(tools, "_ask", fake_ask)

    result = tools.brain_ask("memory query", brain_root=tmp_path)

    assert result == {"query": "memory query", "mode": "keyword-only", "results": []}
    assert calls == [
        {
            "root": tmp_path,
            "query": "memory query",
            "top": 5,
            "page_type": None,
            "explain": False,
            "show_sql": False,
            "mode": "keyword-only",
            "debug": False,
        }
    ]


def test_brain_capture_writes_laundry_without_autocommit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_capture(root: Path, text: str, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, "text": text, **kwargs})
        return {"path": "laundry/capture.md", "kind": kwargs["kind"], "committed": False}

    monkeypatch.setattr(tools, "_capture", fake_capture)

    result = tools.brain_capture(
        "raw note",
        brain_root=tmp_path,
        kind="idea",
        source_context="unit test",
    )

    assert result["path"] == "laundry/capture.md"
    assert calls == [
        {
            "root": tmp_path,
            "text": "source_agent: mcp\nsource_context: unit test\nsource_channel: stdin\n\nraw note",
            "kind": "idea",
            "source": "stdin",
            "source_ref": None,
            "auto_commit": False,
        }
    ]


def test_brain_inject_uses_pipeline_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_inject(root: Path, query: str, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, "query": query, **kwargs})
        return {"content": "context", "mode": kwargs["mode"]}

    fake_module = types.SimpleNamespace(inject=fake_inject)
    monkeypatch.setitem(sys.modules, "brain.pipeline.injection", fake_module)

    result = tools.brain_inject("task", brain_root=tmp_path, budget=1200)

    assert result == {"content": "context", "mode": "keyword-only"}
    assert calls == [
        {
            "root": tmp_path,
            "query": "task",
            "budget": 1200,
            "output_format": "markdown",
            "mode": "keyword-only",
            "top": tools.DEFAULT_INJECT_TOP,
            "include_snapshot": True,
            "page_type": None,
            "include_slugs": [],
        }
    ]


def test_brain_inject_default_budget_matches_pipeline_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_inject(root: Path, query: str, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, "query": query, **kwargs})
        return {"budget": kwargs["budget"]}

    fake_module = types.SimpleNamespace(inject=fake_inject)
    monkeypatch.setitem(sys.modules, "brain.pipeline.injection", fake_module)

    result = tools.brain_inject("task", brain_root=tmp_path)

    assert result == {"budget": 10000}
    assert calls == [
        {
            "root": tmp_path,
            "query": "task",
            "budget": 10000,
            "output_format": "markdown",
            "mode": "keyword-only",
            "top": tools.DEFAULT_INJECT_TOP,
            "include_snapshot": True,
            "page_type": None,
            "include_slugs": [],
        }
    ]


def test_brain_inject_can_disable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_inject(root: Path, query: str, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, "query": query, **kwargs})
        return {"include_snapshot": kwargs["include_snapshot"]}

    fake_module = types.SimpleNamespace(inject=fake_inject)
    monkeypatch.setitem(sys.modules, "brain.pipeline.injection", fake_module)

    result = tools.brain_inject("task", brain_root=tmp_path, include_snapshot=False)

    assert result == {"include_snapshot": False}
    assert calls[0]["include_snapshot"] is False


def test_brain_capture_preserves_existing_source_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_capture(root: Path, text: str, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, "text": text, **kwargs})
        return {"path": "laundry/capture.md"}

    monkeypatch.setattr(tools, "_capture", fake_capture)

    text = "source_agent: codex\nsource_context: task\n\nraw note"
    tools.brain_capture(text, brain_root=tmp_path)

    assert calls[0]["text"] == text


def test_brain_capture_does_not_accept_incidental_source_words(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_capture(root: Path, text: str, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, "text": text, **kwargs})
        return {"path": "laundry/capture.md"}

    monkeypatch.setattr(tools, "_capture", fake_capture)

    text = "This note mentions source_agent: and source_context: in the body."
    tools.brain_capture(text, brain_root=tmp_path, source_context="unit test")

    assert calls[0]["text"].startswith("source_agent: mcp\nsource_context: unit test\n")


def test_brain_capture_requires_source_context_without_existing_context() -> None:
    with pytest.raises(ValueError, match="source_context is required"):
        tools.brain_capture("raw note")


def test_brain_inject_passes_page_type_and_include_slug(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_inject(root: Path, query: str, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, "query": query, **kwargs})
        return {"page_type": kwargs["page_type"], "include_slugs": kwargs["include_slugs"]}

    fake_module = types.SimpleNamespace(inject=fake_inject)
    monkeypatch.setitem(sys.modules, "brain.pipeline.injection", fake_module)

    result = tools.brain_inject(
        "task",
        brain_root=tmp_path,
        page_type="procedure",
        include_slug=["deploy"],
    )

    assert result == {"page_type": "procedure", "include_slugs": ["deploy"]}


def test_brain_scratch_append_uses_pipeline_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_append_working(root: Path, text: str, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, "text": text, **kwargs})
        return {"path": "scratch/working.md", "source": kwargs["source"]}

    fake_module = types.SimpleNamespace(append_working=fake_append_working)
    monkeypatch.setitem(sys.modules, "brain.pipeline.scratch", fake_module)

    result = tools.brain_scratch_append("working note", brain_root=tmp_path, source="codex")

    assert result == {"path": "scratch/working.md", "source": "codex"}
    assert calls == [{"root": tmp_path, "text": "working note", "source": "codex"}]


def test_brain_snapshot_rebuild_uses_pipeline_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_rebuild_snapshot(root: Path, **kwargs: object) -> dict[str, object]:
        calls.append({"root": root, **kwargs})
        return {"path": "scratch/SNAPSHOT.md", "entries": 2}

    fake_module = types.SimpleNamespace(rebuild_snapshot=fake_rebuild_snapshot)
    monkeypatch.setitem(sys.modules, "brain.pipeline.scratch", fake_module)

    result = tools.brain_snapshot_rebuild(tmp_path, max_items=2, max_chars=1200)

    assert result == {"path": "scratch/SNAPSHOT.md", "entries": 2}
    assert calls == [{"root": tmp_path, "max_items": 2, "max_chars": 1200}]


def test_brain_procedure_list_reads_procedure_pages(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    procedure_path = root / "pages" / "procedures" / "deploy.md"
    write_page(
        procedure_path,
        Page(
            frontmatter=Frontmatter(
                type=PageType.PROCEDURE,
                slug="deploy",
                title="Deploy",
                created=datetime(2026, 5, 10, tzinfo=UTC),
                updated=datetime(2026, 5, 10, tzinfo=UTC),
                status=ProcedureStatus.STABLE,
                success_count=5,
                fail_count=0,
            ),
            compiled_truth="Deploy procedure.",
            timeline=[],
            sources=[],
        ),
    )

    result = tools.brain_procedure_list(root, status="stable")

    assert result["count"] == 1
    assert result["procedures"][0]["slug"] == "deploy"
    assert result["procedures"][0]["status"] == "stable"
    assert result["procedures"][0]["path"] == "pages/procedures/deploy.md"


def test_brain_procedure_list_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        tools.brain_procedure_list(limit=0)


def test_brain_procedure_new_run_promote_use_pipeline_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_create(root: Path, slug: str, **kwargs: object) -> dict[str, object]:
        calls.append({"fn": "new", "root": root, "slug": slug, **kwargs})
        return {"slug": slug, "status": "raw"}

    def fake_run(root: Path, slug: str, **kwargs: object) -> dict[str, object]:
        calls.append({"fn": "run", "root": root, "slug": slug, **kwargs})
        return {"slug": slug, "status": "tested"}

    def fake_promote(root: Path, slug: str, **kwargs: object) -> dict[str, object]:
        calls.append({"fn": "promote", "root": root, "slug": slug, **kwargs})
        return {"slug": slug, "status": kwargs["status"]}

    fake_module = types.SimpleNamespace(
        create_procedure=fake_create,
        run_procedure=fake_run,
        promote_procedure=fake_promote,
    )
    monkeypatch.setitem(sys.modules, "brain.pipeline.procedure", fake_module)

    assert tools.brain_procedure_new("deploy", "Deploy", brain_root=tmp_path)["status"] == "raw"
    assert (
        tools.brain_procedure_run("deploy", "success", "ok", brain_root=tmp_path)["status"]
        == "tested"
    )
    assert (
        tools.brain_procedure_promote("deploy", "stable", brain_root=tmp_path)["status"]
        == "stable"
    )
    assert calls == [
        {
            "fn": "new",
            "root": tmp_path,
            "slug": "deploy",
            "title": "Deploy",
            "auto_commit": False,
        },
        {
            "fn": "run",
            "root": tmp_path,
            "slug": "deploy",
            "result": "success",
            "note": "ok",
            "auto_commit": False,
        },
        {
            "fn": "promote",
            "root": tmp_path,
            "slug": "deploy",
            "status": "stable",
            "auto_commit": False,
        },
    ]


def test_brain_review_queue_lists_pending_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, str | None]] = []

    def fake_list_pending(root: Path, kind: str | None = None) -> list[dict[str, str]]:
        calls.append((root, kind))
        return [{"review_id": "one", "status": "pending"}]

    monkeypatch.setattr(tools, "_list_pending", fake_list_pending)

    result = tools.brain_review_queue(tmp_path, kind="pending_fact")

    assert result == {"items": [{"review_id": "one", "status": "pending"}], "count": 1}
    assert calls == [(tmp_path, "pending_fact")]


def test_brain_recent_events_returns_newest_first(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    root.mkdir()
    events_path = root / "events.jsonl"
    events = [
        Event(
            id=VALID_ULID,
            timestamp=datetime(2026, 4, 28, tzinfo=UTC),
            kind=EventKind.NOTE_APPENDED,
            source_ref="first",
        ),
        Event(
            id=SECOND_ULID,
            timestamp=datetime(2026, 4, 29, tzinfo=UTC),
            kind=EventKind.AI_CHAT,
            source_ref="second",
        ),
    ]
    events_path.write_text(
        "\n".join(event.model_dump_json() for event in events) + "\n",
        encoding="utf-8",
    )

    result = tools.brain_recent_events(root, limit=1)

    assert result["count"] == 1
    assert result["events"][0]["id"] == SECOND_ULID


def test_brain_recent_events_filters_kind(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    root.mkdir()
    events_path = root / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                Event(
                    id=VALID_ULID,
                    timestamp=datetime(2026, 4, 28, tzinfo=UTC),
                    kind=EventKind.NOTE_APPENDED,
                    source_ref="first",
                ).model_dump_json(),
                Event(
                    id=SECOND_ULID,
                    timestamp=datetime(2026, 4, 29, tzinfo=UTC),
                    kind=EventKind.AI_CHAT,
                    source_ref="second",
                ).model_dump_json(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = tools.brain_recent_events(root, kind="note_appended")

    assert result["count"] == 1
    assert result["events"][0]["source_ref"] == "first"


def test_brain_recent_events_filters_entity_slug(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    root.mkdir()
    events_path = root / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                Event(
                    id=VALID_ULID,
                    timestamp=datetime(2026, 4, 28, tzinfo=UTC),
                    kind=EventKind.NOTE_APPENDED,
                    source_ref="first",
                    affected_pages=["alice"],
                ).model_dump_json(),
                Event(
                    id=SECOND_ULID,
                    timestamp=datetime(2026, 4, 29, tzinfo=UTC),
                    kind=EventKind.AI_CHAT,
                    source_ref="second",
                    affected_pages=["bob"],
                ).model_dump_json(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = tools.brain_recent_events(root, entity_slug="alice")

    assert result["count"] == 1
    assert result["events"][0]["source_ref"] == "first"


def test_brain_recent_events_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        tools.brain_recent_events(limit=0)
