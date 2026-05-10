from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.mcp import tools
from brain.models.event import Event, EventKind

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
        brain_recent_events,
        brain_review_queue,
        brain_status,
    )

    assert brain_status
    assert brain_ask
    assert brain_capture
    assert brain_inject
    assert brain_review_queue
    assert brain_recent_events


def test_mcp_tool_docstrings_explain_when_to_call() -> None:
    tool_functions = [
        tools.brain_status,
        tools.brain_ask,
        tools.brain_capture,
        tools.brain_inject,
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

    result = tools.brain_capture("raw note", brain_root=tmp_path, kind="idea")

    assert result["path"] == "laundry/capture.md"
    assert calls == [
        {
            "root": tmp_path,
            "text": "raw note",
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


def test_brain_recent_events_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        tools.brain_recent_events(limit=0)
