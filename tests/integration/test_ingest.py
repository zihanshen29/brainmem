from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from brain.cli.init import init_brain
from brain.cli.main import app
from brain.db.connection import connect
from brain.db.facts import add_fact
from brain.exceptions import BrainError
from brain.ledger import append_event, read_all
from brain.llm import client as llm_client
from brain.models import (
    EntityType,
    Event,
    EventKind,
    Fact,
    FactCandidate,
    FactObjectType,
    PageType,
)
from brain.pages import parse_page
from brain.pipeline import signal_detect
from brain.pipeline.signal_detect import SignalEntity, SignalExtraction

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


@pytest.fixture()
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    return root


def test_laundry_ingest_persists_fact_page_timeline_archive_and_cursor(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    laundry_path = _write_laundry(brain_root, "Alice started maintaining Brain.")
    _install_signal_detector(monkeypatch, lambda: _alice_extraction())

    report = _run_ingest(brain_root, source="laundry")

    assert report.processed == 1
    assert report.facts_added == 1
    assert report.review_items_created == 0
    assert report.laundry_archived == 1

    facts = _rows(brain_root, "SELECT * FROM facts")
    assert len(facts) == 1
    assert facts[0]["subject"] == "alice"
    assert facts[0]["predicate"] == "role"
    assert facts[0]["object"] == "memory-system-maintainer"

    laundry_events = [
        event
        for event in read_all(brain_root / "events.jsonl")
        if event.kind == EventKind.LAUNDRY_INGESTED
    ]
    assert len(laundry_events) == 1

    page_path = brain_root / "pages" / "entities" / "alice.md"
    page = parse_page(page_path)
    assert page.frontmatter.slug == "alice"
    assert page.frontmatter.title == "Alice"
    assert any(laundry_events[0].id in entry for entry in page.timeline)
    assert any("Alice started maintaining Brain." in entry for entry in page.timeline)

    assert not laundry_path.exists()
    archived = list((brain_root / "laundry" / "processed").glob("*.md"))
    assert len(archived) == 1
    assert "Alice started maintaining Brain." in archived[0].read_text(encoding="utf-8")

    cursor = _rows(
        brain_root,
        "SELECT source, last_processed, last_run_at FROM ingest_cursor WHERE source = ?",
        ("laundry",),
    )
    assert len(cursor) == 1
    assert cursor[0]["last_processed"]
    assert cursor[0]["last_run_at"]
    assert not (brain_root / "brain.db-wal").exists()
    assert not (brain_root / "brain.db-shm").exists()


def test_second_ingest_does_not_reprocess_archived_laundry(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_laundry(brain_root, "Alice started maintaining Brain.")
    detector_calls = _install_signal_detector(monkeypatch, lambda: _alice_extraction())

    first_report = _run_ingest(brain_root, source="laundry")
    fact_count = _scalar(brain_root, "SELECT COUNT(*) FROM facts")
    event_count = len(list(read_all(brain_root / "events.jsonl")))

    second_report = _run_ingest(brain_root, source="laundry")

    assert first_report.processed == 1
    assert second_report.processed == 0
    assert _scalar(brain_root, "SELECT COUNT(*) FROM facts") == fact_count
    assert len(list(read_all(brain_root / "events.jsonl"))) == event_count
    assert len(detector_calls) == 1


def test_events_ingest_uses_cursor_to_skip_processed_events(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    append_event(
        brain_root / "events.jsonl",
        Event(
            id=VALID_ULID,
            timestamp=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
            kind=EventKind.RAW_IMPORTED,
            source_ref="raw/alice.md",
            raw_payload="Alice started maintaining Brain.",
        ),
    )
    detector_calls = _install_signal_detector(monkeypatch, lambda: _alice_extraction())

    first_report = _run_ingest(brain_root, source="events")
    fact_count = _scalar(brain_root, "SELECT COUNT(*) FROM facts")
    second_report = _run_ingest(brain_root, source="events")

    cursor = _rows(
        brain_root,
        "SELECT last_processed FROM ingest_cursor WHERE source = ?",
        ("events",),
    )

    assert first_report.processed == 1
    assert second_report.processed == 0
    assert _scalar(brain_root, "SELECT COUNT(*) FROM facts") == fact_count
    assert len(detector_calls) == 1
    assert cursor[0]["last_processed"] == VALID_ULID


def test_conflicting_fact_creates_fact_conflict_review(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_fact(brain_root, object_value="designer")
    _write_laundry(brain_root, "Alice is now an engineer.")
    _install_signal_detector(
        monkeypatch,
        lambda: _alice_extraction(object_value="engineer", summary="Alice is now an engineer."),
    )

    report = _run_ingest(brain_root, source="laundry")

    assert report.facts_added == 0
    assert report.review_items_created == 1
    assert _scalar(brain_root, "SELECT COUNT(*) FROM facts") == 1

    review_files = [
        path for path in (brain_root / "review").glob("*.md") if path.parent.name != "archive"
    ]
    assert len(review_files) == 1
    review_text = review_files[0].read_text(encoding="utf-8")
    assert "fact_conflict" in review_text
    assert "designer" in review_text
    assert "engineer" in review_text
    assert "## Decision" in review_text
    assert "[ ] approve" in review_text
    assert "[ ] reject" in review_text
    assert "[ ] defer" in review_text


def test_dry_run_does_not_write_db_files_archive_events_cursor_or_git(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    laundry_path = _write_laundry(brain_root, "Alice started maintaining Brain.")
    _install_signal_detector(monkeypatch, lambda: _alice_extraction())
    before_snapshot = _file_snapshot(brain_root)
    before_head = _git_output(brain_root, "rev-parse", "HEAD")
    before_status = _git_output(brain_root, "status", "--short")

    report = _run_ingest(brain_root, source="laundry", dry_run=True)

    assert report.dry_run is True
    assert _file_snapshot(brain_root) == before_snapshot
    assert laundry_path.exists()
    assert list((brain_root / "laundry" / "processed").glob("*.md")) == []
    assert _scalar(brain_root, "SELECT COUNT(*) FROM facts") == 0
    assert _scalar(brain_root, "SELECT COUNT(*) FROM ingest_cursor") == 0
    assert list(read_all(brain_root / "events.jsonl")) == []
    assert _git_output(brain_root, "rev-parse", "HEAD") == before_head
    assert _git_output(brain_root, "status", "--short") == before_status


def test_cli_ingest_dry_run_and_source_laundry_output_summary(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_laundry(brain_root, "Alice started maintaining Brain.")
    _install_signal_detector(monkeypatch, lambda: _alice_extraction())
    monkeypatch.chdir(brain_root)

    dry_run_result = runner.invoke(app, ["ingest", "--dry-run"])
    source_result = runner.invoke(app, ["ingest", "--source", "laundry"])

    assert dry_run_result.exit_code == 0
    assert "Ingest summary:" in dry_run_result.stdout
    assert "processed=" in dry_run_result.stdout
    assert "facts_added=" in dry_run_result.stdout
    assert "review_items_created=" in dry_run_result.stdout
    assert "laundry_archived=" in dry_run_result.stdout
    assert "dry_run=true" in dry_run_result.stdout

    assert source_result.exit_code == 0
    assert "Ingest summary:" in source_result.stdout
    assert "processed=1" in source_result.stdout
    assert "facts_added=1" in source_result.stdout
    assert "review_items_created=0" in source_result.stdout
    assert "laundry_archived=1" in source_result.stdout
    assert "dry_run=false" in source_result.stdout


def test_cli_ingest_brain_error_outputs_stderr_and_exit_one(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain.cli import ingest as ingest_cli

    def raise_brain_error(*_args: object, **_kwargs: object) -> None:
        raise BrainError("ingest failed")

    monkeypatch.setattr(ingest_cli, "_run_ingest", raise_brain_error)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["ingest"])

    assert result.exit_code == 1
    assert "Error: ingest failed" in result.stderr


def _run_ingest(
    root: Path,
    *,
    source: str = "all",
    dry_run: bool = False,
    limit: int | None = None,
) -> Any:
    ingest_module = importlib.import_module("brain.pipeline.ingest")
    return ingest_module.ingest(root, source=source, dry_run=dry_run, limit=limit)


def _install_signal_detector(
    monkeypatch: pytest.MonkeyPatch,
    extraction_factory: Callable[[], SignalExtraction],
) -> list[tuple[str, str, dict[str, Any] | None]]:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_detect_signal(text: str, hint: dict[str, Any] | None = None) -> SignalExtraction:
        calls.append(("detect_signal", text, hint))
        return extraction_factory()

    def fake_extract_signal(text: str) -> dict[str, Any]:
        calls.append(("extract_signal", text, None))
        return extraction_factory().model_dump(mode="json")

    monkeypatch.setattr(signal_detect, "detect_signal", fake_detect_signal)
    monkeypatch.setattr(llm_client, "extract_signal", fake_extract_signal)

    ingest_module = importlib.import_module("brain.pipeline.ingest")
    for name in ("detect_signal", "_detect_signal"):
        if hasattr(ingest_module, name):
            monkeypatch.setattr(ingest_module, name, fake_detect_signal)
    for name in ("extract_signal", "_extract_signal"):
        if hasattr(ingest_module, name):
            monkeypatch.setattr(ingest_module, name, fake_extract_signal)

    return calls


def _alice_extraction(
    *,
    object_value: str = "memory-system-maintainer",
    confidence: float = 0.9,
    summary: str = "Alice started maintaining Brain.",
) -> SignalExtraction:
    return SignalExtraction(
        entities=[
            SignalEntity(
                name="Alice",
                type=EntityType.PERSON,
                confidence=0.95,
                metadata={},
            ),
        ],
        facts=[
            FactCandidate(
                subject="alice",
                predicate="role",
                object=object_value,
                object_type=FactObjectType.LITERAL,
                valid_from="2026-04-28",
                valid_to=None,
                source_event=SECOND_ULID,
                source_ref="laundry/alice.md",
                confidence=confidence,
            ),
        ],
        timeline_summary=summary,
        suggested_page_type=PageType.ENTITY,
    )


def _insert_fact(brain_root: Path, *, object_value: str) -> None:
    with connect(brain_root / "brain.db") as conn:
        add_fact(
            conn,
            Fact(
                subject="alice",
                predicate="role",
                object=object_value,
                object_type=FactObjectType.LITERAL,
                valid_from="2026-04-01",
                valid_to=None,
                asserted_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
                source_event=VALID_ULID,
                source_ref="events.jsonl",
                confidence=0.9,
            ),
        )
        conn.commit()


def _write_laundry(brain_root: Path, text: str) -> Path:
    path = brain_root / "laundry" / "alice.md"
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
    return path


def _rows(brain_root: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    uri = f"file:{(brain_root / 'brain.db').as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _scalar(brain_root: Path, sql: str, params: tuple[object, ...] = ()) -> Any:
    return _rows(brain_root, sql, params)[0][0]


def _file_snapshot(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not path.is_file() or ".git" in relative.parts:
            continue
        files[relative.as_posix()] = path.read_bytes()
    return files


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        encoding="utf-8",
        env=_git_env(),
        check=True,
    )
    return result.stdout


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env
