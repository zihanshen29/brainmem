from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from brain.cli.main import app
from brain.db.connection import connect
from brain.db.facts import add_fact
from brain.exceptions import BrainError, IngestError, LLMError
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
from brain.pipeline.signal_detect import ProcedureCandidate, SignalEntity, SignalExtraction

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"

runner = CliRunner()
pytestmark = pytest.mark.usefixtures("fake_provider_key")


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


def test_project_entity_uses_project_page_directory(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_laundry(brain_root, "Brain Project shipped a milestone.")
    _install_signal_detector(
        monkeypatch,
        lambda: SignalExtraction(
            entities=[
                SignalEntity(
                    name="Brain Project",
                    type=EntityType.PROJECT,
                    confidence=0.95,
                    metadata={},
                ),
            ],
            facts=[
                FactCandidate(
                    subject="brain-project",
                    predicate="status",
                    object="milestone-shipped",
                    object_type=FactObjectType.LITERAL,
                    valid_from="2026-04-28",
                    source_event=SECOND_ULID,
                    source_ref="laundry/brain.md",
                    confidence=0.9,
                ),
            ],
            timeline_summary="Brain Project shipped a milestone.",
            suggested_page_type=PageType.PROJECT,
        ),
    )

    report = _run_ingest(brain_root, source="laundry")

    assert report.facts_added == 1
    project_path = brain_root / "pages" / "projects" / "brain-project.md"
    page = parse_page(project_path)
    assert page.frontmatter.type is PageType.PROJECT
    assert page.frontmatter.tier is None
    assert not (brain_root / "pages" / "entities" / "brain-project.md").exists()


def test_transient_entity_delays_stub_page_until_repeated_evidence(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extractions = [
        _transient_extraction(predicate="stage", object_value="first-seen"),
        _transient_extraction(predicate="status", object_value="confirmed"),
    ]
    _install_signal_detector(monkeypatch, lambda: extractions.pop(0))

    _write_laundry(brain_root, "Smoke Verification appeared once.")
    first_report = _run_ingest(brain_root, source="laundry")
    page_path = brain_root / "pages" / "concepts" / "smoke-verification.md"

    assert first_report.facts_added == 1
    assert first_report.pages_touched == []
    assert not page_path.exists()

    _write_laundry(brain_root, "Smoke Verification appeared again.")
    second_report = _run_ingest(brain_root, source="laundry")

    assert second_report.facts_added == 1
    assert second_report.pages_touched == ["pages/concepts/smoke-verification.md"]
    assert page_path.exists()


def test_high_confidence_procedure_candidate_creates_review(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    laundry_path = _write_laundry(
        brain_root,
        "Procedure: run daily review by listing pending items and resolving them.",
    )
    _install_signal_detector(monkeypatch, lambda: _procedure_extraction(confidence=0.91))

    report = _run_ingest(brain_root, source="laundry")

    review_files = list((brain_root / "review").glob("*.md"))
    assert report.processed == 1
    assert report.facts_added == 0
    assert report.review_items_created == 1
    assert report.laundry_archived == 1
    assert len(review_files) == 1
    assert not (brain_root / "pages" / "procedures" / "daily-review.md").exists()
    review_text = review_files[0].read_text(encoding="utf-8")
    assert "kind: procedure_candidate" in review_text
    assert "- reason: candidate" in review_text
    assert '"suggested_slug": "daily-review"' in review_text
    assert not laundry_path.exists()


def test_low_confidence_procedure_candidate_creates_review(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_laundry(
        brain_root,
        "Possible procedure: run daily review by checking pending items.",
    )
    _install_signal_detector(monkeypatch, lambda: _procedure_extraction(confidence=0.5))

    report = _run_ingest(brain_root, source="laundry")

    review_files = list((brain_root / "review").glob("*.md"))
    assert report.processed == 1
    assert report.facts_added == 0
    assert report.review_items_created == 1
    assert len(review_files) == 1
    assert not (brain_root / "pages" / "procedures" / "daily-review.md").exists()

    review_text = review_files[0].read_text(encoding="utf-8")
    assert "kind: procedure_candidate" in review_text
    assert "# Procedure candidate" in review_text
    assert "- reason: low_confidence" in review_text
    assert '"suggested_slug": "daily-review"' in review_text
    assert "## Decision" in review_text


def test_unresolved_entity_fact_becomes_pending_fact_review(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_laundry(brain_root, "小张 will work on recommendations.")
    _install_signal_detector(
        monkeypatch,
        lambda: SignalExtraction(
            entities=[
                SignalEntity(
                    name="小张",
                    type=EntityType.PERSON,
                    confidence=0.95,
                    metadata={},
                ),
            ],
            facts=[
                FactCandidate(
                    subject="小张",
                    predicate="works_on",
                    object="recommendations",
                    object_type=FactObjectType.LITERAL,
                    valid_from="2026-04-28",
                    source_event=SECOND_ULID,
                    source_ref="laundry/xiao-zhang.md",
                    confidence=0.9,
                ),
            ],
            timeline_summary="小张 will work on recommendations.",
            suggested_page_type=PageType.ENTITY,
        ),
    )

    report = _run_ingest(brain_root, source="laundry")

    review_texts = [
        path.read_text(encoding="utf-8") for path in sorted((brain_root / "review").glob("*.md"))
    ]
    assert report.processed == 1
    assert report.facts_added == 0
    assert report.review_items_created == 2
    assert report.laundry_archived == 1
    assert any("kind: new_entity_review" in text for text in review_texts)
    assert any("kind: pending_fact" in text for text in review_texts)
    assert any('"subject": "小张"' in text for text in review_texts)


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


def test_ingest_default_auto_reindex_runs_for_touched_pages(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_laundry(brain_root, "Alice started maintaining Brain.")
    _install_signal_detector(monkeypatch, lambda: _alice_extraction())
    reindex_calls = _install_reindex_stub(monkeypatch)

    report = _run_ingest(brain_root, source="laundry", auto_reindex=None)

    assert report.processed == 1
    assert report.errors == []
    assert reindex_calls == [
        {
            "brain_root": brain_root,
            "page_filter": ["alice"],
            "no_commit": True,
        }
    ]


def test_ingest_auto_reindex_failure_is_reported_without_breaking_ingest(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_laundry(brain_root, "Alice started maintaining Brain.")
    _install_signal_detector(monkeypatch, lambda: _alice_extraction())
    ingest_module = importlib.import_module("brain.pipeline.ingest")

    def fail_reindex(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("embedding service unavailable")

    monkeypatch.setattr(ingest_module, "reindex", fail_reindex)

    report = _run_ingest(brain_root, source="laundry", auto_reindex=None)

    assert report.processed == 1
    assert report.facts_added == 1
    assert report.errors == ["auto-reindex failed: embedding service unavailable"]


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


def test_events_ingest_skips_events_without_payload(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    append_event(
        brain_root / "events.jsonl",
        Event(
            id=VALID_ULID,
            timestamp=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
            kind=EventKind.REINDEXED,
            source_ref="reindex",
        ),
    )
    append_event(
        brain_root / "events.jsonl",
        Event(
            id=SECOND_ULID,
            timestamp=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
            kind=EventKind.RAW_IMPORTED,
            source_ref="raw/alice.md",
            raw_payload="Alice started maintaining Brain.",
        ),
    )
    detector_calls = _install_signal_detector(monkeypatch, lambda: _alice_extraction())

    report = _run_ingest(brain_root, source="events")

    cursor = _rows(
        brain_root,
        "SELECT last_processed FROM ingest_cursor WHERE source = ?",
        ("events",),
    )

    assert report.processed == 1
    assert report.errors == []
    assert len(detector_calls) == 1
    assert cursor[0]["last_processed"] == SECOND_ULID


def test_events_ingest_failure_writes_review_and_advances_cursor(
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

    def raise_detection_error() -> SignalExtraction:
        raise RuntimeError("detector unavailable")

    _install_signal_detector(monkeypatch, raise_detection_error)

    report = _run_ingest(brain_root, source="events")

    cursor = _rows(
        brain_root,
        "SELECT last_processed FROM ingest_cursor WHERE source = ?",
        ("events",),
    )
    review_files = list((brain_root / "review").glob("*.md"))

    assert report.processed == 0
    assert report.facts_added == 0
    assert report.review_items_created == 1
    assert len(report.errors) == 1
    assert cursor[0]["last_processed"] == VALID_ULID
    assert len(review_files) == 1

    review_text = review_files[0].read_text(encoding="utf-8")
    assert "kind: ingest_error" in review_text
    assert "detector unavailable" in review_text
    assert VALID_ULID in review_text
    assert report.review_files == [review_files[0].relative_to(brain_root).as_posix()]


def test_laundry_ingest_failure_writes_review_and_moves_to_failed(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    laundry_path = _write_laundry(brain_root, "Alice started maintaining Brain.")

    def raise_detection_error() -> SignalExtraction:
        raise RuntimeError("detector unavailable")

    _install_signal_detector(monkeypatch, raise_detection_error)

    report = _run_ingest(brain_root, source="laundry")

    failed_path = brain_root / "laundry" / "failed" / laundry_path.name
    review_files = list((brain_root / "review").glob("*.md"))

    assert report.processed == 0
    assert report.facts_added == 0
    assert report.review_items_created == 1
    assert report.laundry_archived == 0
    assert not laundry_path.exists()
    assert failed_path.exists()
    assert len(review_files) == 1
    review_text = review_files[0].read_text(encoding="utf-8")
    assert "kind: ingest_error" in review_text
    assert "- failure_kind: content" in review_text


def test_missing_provider_key_aborts_before_reviews_or_laundry_moves(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    laundry_path = _write_laundry(brain_root, "Alice started maintaining Brain.")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def unexpected_detector(*_args: object, **_kwargs: object) -> SignalExtraction:
        raise AssertionError("provider preflight should run before detection")

    ingest_module = importlib.import_module("brain.pipeline.ingest")
    monkeypatch.setattr(ingest_module, "detect_signal", unexpected_detector)

    with pytest.raises(IngestError, match="provider preflight failed"):
        _run_ingest(brain_root, source="laundry")

    assert laundry_path.exists()
    assert not (brain_root / "laundry" / "failed" / laundry_path.name).exists()
    assert list((brain_root / "review").glob("*.md")) == []
    assert list(read_all(brain_root / "events.jsonl")) == []
    assert _scalar(brain_root, "SELECT COUNT(*) FROM facts") == 0


def test_invalid_provider_endpoint_aborts_before_detection(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    laundry_path = _write_laundry(brain_root, "Alice started maintaining Brain.")
    config_path = brain_root / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'base_url = "https://api.deepseek.com"',
            'base_url = "not-a-url"',
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )

    def unexpected_detector(*_args: object, **_kwargs: object) -> SignalExtraction:
        raise AssertionError("provider preflight should run before detection")

    ingest_module = importlib.import_module("brain.pipeline.ingest")
    monkeypatch.setattr(ingest_module, "detect_signal", unexpected_detector)

    with pytest.raises(IngestError, match="base_url"):
        _run_ingest(brain_root, source="laundry")

    assert laundry_path.exists()
    assert list((brain_root / "review").glob("*.md")) == []


def test_transient_provider_failure_aborts_staged_batch_without_partial_writes(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice_path = _write_laundry(brain_root, "Alice started maintaining Brain.")
    bob_path = brain_root / "laundry" / "bob.md"
    bob_path.write_text("Bob started maintaining Brain.\n", encoding="utf-8", newline="\n")
    detector_calls = 0

    def fake_detect_signal(
        text: str,
        hint: dict[str, Any] | None = None,
    ) -> SignalExtraction:
        nonlocal detector_calls
        del hint
        detector_calls += 1
        if text.startswith("Alice"):
            return _alice_extraction()
        try:
            raise TimeoutError("provider timed out")
        except TimeoutError as exc:
            raise LLMError("LLM API call failed") from exc

    ingest_module = importlib.import_module("brain.pipeline.ingest")
    monkeypatch.setattr(ingest_module, "detect_signal", fake_detect_signal)

    with pytest.raises(IngestError, match="temporary provider"):
        _run_ingest(brain_root, source="laundry")

    assert detector_calls == 2
    assert alice_path.exists()
    assert bob_path.exists()
    assert list((brain_root / "laundry" / "processed").glob("*.md")) == []
    assert list((brain_root / "laundry" / "failed").glob("*.md")) == []
    assert list((brain_root / "review").glob("*.md")) == []
    assert list(read_all(brain_root / "events.jsonl")) == []
    assert _scalar(brain_root, "SELECT COUNT(*) FROM facts") == 0


def test_transient_conflict_judgment_keeps_current_source_without_ingest_error(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_fact(brain_root, object_value="designer", confidence=0.6)
    laundry_path = _write_laundry(brain_root, "Alice is now an engineer.")
    _install_signal_detector(
        monkeypatch,
        lambda: _alice_extraction(object_value="engineer", summary="Alice is now an engineer."),
    )

    def fail_conflict_judgment(*_args: object, **_kwargs: object) -> None:
        try:
            raise TimeoutError("conflict provider timed out")
        except TimeoutError as exc:
            raise LLMError("LLM API call failed") from exc

    monkeypatch.setattr(llm_client, "judge_conflict", fail_conflict_judgment)

    with pytest.raises(IngestError, match="temporary provider"):
        _run_ingest(brain_root, source="laundry")

    assert laundry_path.exists()
    assert list((brain_root / "laundry" / "failed").glob("*.md")) == []
    ingest_error_reviews = [
        path
        for path in (brain_root / "review").glob("*.md")
        if "kind: ingest_error" in path.read_text(encoding="utf-8")
    ]
    assert ingest_error_reviews == []
    assert _scalar(brain_root, "SELECT COUNT(*) FROM facts") == 1


def test_requeue_failed_laundry_preserves_colliding_pending_files(
    brain_root: Path,
) -> None:
    ingest_module = importlib.import_module("brain.pipeline.ingest")
    pending = brain_root / "laundry" / "alice.md"
    pending.write_text("already pending\n", encoding="utf-8", newline="\n")
    failed_dir = brain_root / "laundry" / "failed"
    nested_dir = failed_dir / "older"
    nested_dir.mkdir(parents=True)
    first_failed = failed_dir / "alice.md"
    second_failed = nested_dir / "alice.md"
    first_failed.write_text("first failure\n", encoding="utf-8", newline="\n")
    second_failed.write_text("second failure\n", encoding="utf-8", newline="\n")

    report = ingest_module.requeue_failed_laundry(brain_root)

    assert report.requeued == 2
    assert report.files == ["laundry/alice_1.md", "laundry/alice_2.md"]
    assert pending.read_text(encoding="utf-8") == "already pending\n"
    assert (brain_root / "laundry" / "alice_1.md").read_text(encoding="utf-8") == (
        "first failure\n"
    )
    assert (brain_root / "laundry" / "alice_2.md").read_text(encoding="utf-8") == (
        "second failure\n"
    )
    assert not first_failed.exists()
    assert not second_failed.exists()


def test_requeue_failed_laundry_requires_positive_limit(brain_root: Path) -> None:
    ingest_module = importlib.import_module("brain.pipeline.ingest")

    with pytest.raises(IngestError, match="limit must be positive"):
        ingest_module.requeue_failed_laundry(brain_root, limit=0)


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


def test_dry_run_is_local_and_does_not_require_provider_key(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    laundry_path = _write_laundry(brain_root, "Alice started maintaining Brain.")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def unexpected_detector(*_args: object, **_kwargs: object) -> SignalExtraction:
        raise AssertionError("dry-run must not call the provider-backed detector")

    ingest_module = importlib.import_module("brain.pipeline.ingest")
    monkeypatch.setattr(ingest_module, "detect_signal", unexpected_detector)

    report = _run_ingest(brain_root, source="laundry", dry_run=True)

    assert report.dry_run is True
    assert report.processed == 1
    assert report.errors == []
    assert laundry_path.exists()
    assert list((brain_root / "review").glob("*.md")) == []


def test_cli_ingest_dry_run_and_source_laundry_output_summary(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_laundry(brain_root, "Alice started maintaining Brain.")
    _install_signal_detector(monkeypatch, lambda: _alice_extraction())
    reindex_calls = _install_reindex_stub(monkeypatch)
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
    assert reindex_calls == [
        {
            "brain_root": brain_root,
            "page_filter": ["alice"],
            "no_commit": True,
        }
    ]


def test_cli_ingest_no_auto_reindex_skips_reindex(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_laundry(brain_root, "Alice started maintaining Brain.")
    _install_signal_detector(monkeypatch, lambda: _alice_extraction())
    reindex_calls = _install_reindex_stub(monkeypatch)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["ingest", "--source", "laundry", "--no-auto-reindex"])

    assert result.exit_code == 0
    assert "Ingest summary:" in result.stdout
    assert reindex_calls == []


def test_cli_ingest_requeue_failed_is_local_and_does_not_overwrite(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = brain_root / "laundry" / "alice.md"
    pending.write_text("pending\n", encoding="utf-8", newline="\n")
    failed_dir = brain_root / "laundry" / "failed"
    failed_dir.mkdir(parents=True)
    failed = failed_dir / "alice.md"
    failed.write_text("failed\n", encoding="utf-8", newline="\n")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = runner.invoke(
        app,
        ["ingest", "--brain-root", str(brain_root), "--requeue-failed", "--verbose"],
    )

    assert result.exit_code == 0
    assert "Requeue summary: requeued=1" in result.stdout
    assert "laundry/alice_1.md" in result.stdout
    assert pending.read_text(encoding="utf-8") == "pending\n"
    assert (brain_root / "laundry" / "alice_1.md").read_text(encoding="utf-8") == ("failed\n")
    assert not failed.exists()


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
    auto_reindex: bool | None = False,
) -> Any:
    ingest_module = importlib.import_module("brain.pipeline.ingest")
    return ingest_module.ingest(
        root,
        source=source,
        dry_run=dry_run,
        limit=limit,
        auto_reindex=auto_reindex,
    )


def _install_reindex_stub(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_reindex(
        brain_root: Path,
        *,
        page_filter: list[str] | None = None,
        no_commit: bool = True,
        **_kwargs: object,
    ) -> SimpleNamespace:
        calls.append(
            {
                "brain_root": Path(brain_root),
                "page_filter": list(page_filter or []),
                "no_commit": no_commit,
            }
        )
        return SimpleNamespace(errors=[])

    ingest_module = importlib.import_module("brain.pipeline.ingest")
    monkeypatch.setattr(ingest_module, "reindex", fake_reindex)
    return calls


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


def _transient_extraction(*, predicate: str, object_value: str) -> SignalExtraction:
    return SignalExtraction(
        entities=[
            SignalEntity(
                name="Smoke Verification",
                type=EntityType.CONCEPT,
                confidence=0.95,
                metadata={},
            ),
        ],
        facts=[
            FactCandidate(
                subject="smoke-verification",
                predicate=predicate,
                object=object_value,
                object_type=FactObjectType.LITERAL,
                valid_from="2026-04-28",
                valid_to=None,
                source_event=SECOND_ULID,
                source_ref="laundry/smoke.md",
                confidence=0.9,
            ),
        ],
        timeline_summary="Smoke Verification was mentioned.",
        suggested_page_type=PageType.CONCEPT,
    )


def _procedure_extraction(*, confidence: float) -> SignalExtraction:
    return SignalExtraction(
        entities=[],
        facts=[],
        procedure_candidates=[
            ProcedureCandidate(
                suggested_slug="daily-review",
                title="Daily Review",
                summary="Review pending memory work each day.",
                steps=[
                    "List pending review items.",
                    "Resolve actionable items.",
                ],
                source_event=SECOND_ULID,
                source_ref="laundry/alice.md",
                confidence=confidence,
                metadata={},
            )
        ],
        timeline_summary="A daily review procedure was described.",
        suggested_page_type=None,
    )


def _insert_fact(
    brain_root: Path,
    *,
    object_value: str,
    confidence: float = 0.9,
) -> None:
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
                confidence=confidence,
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
