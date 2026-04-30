from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli.main import app
from brain.db.connection import connect
from brain.models import Frontmatter, Page, PageType, Tier
from brain.pages import write_page

runner = CliRunner()


def test_status_human_output_contains_minimum_fields(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_status_fixture(brain_root)
    monkeypatch.setattr("brain.pipeline.status.git_ops.is_dirty", lambda _root: True)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    for field in [
        "Brain root:",
        "Pages by type:",
        "Entities by tier:",
        "Facts active/superseded:",
        "Events count:",
        "Pending reviews:",
        "Last ingest:",
        "Git dirty:",
        "Embedding coverage:",
        "Last reindex:",
        "Active import jobs:",
        "Token usage:",
        "Total cost:",
    ]:
        assert field in result.stdout
    assert "Git dirty: true" in result.stdout
    assert "Last ingest: 2026-04-27T10:00:00+00:00" in result.stdout
    assert "Last reindex: 2026-04-27T11:00:00+00:00" in result.stdout


def test_status_json_outputs_stable_count_keys(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_status_fixture(brain_root)
    monkeypatch.setattr("brain.pipeline.status.git_ops.is_dirty", lambda _root: False)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "brain_root",
        "pages_by_type",
        "entities_by_tier",
        "facts_active",
        "facts_superseded",
        "events_count",
        "pending_reviews",
        "last_ingest_at",
        "git_dirty",
        "embedding_coverage",
        "last_reindex_at",
        "active_import_jobs",
        "token_usage",
        "total_cost_usd",
    }
    assert payload["brain_root"] == str(brain_root.resolve())
    assert payload["pages_by_type"] == {
        "entity": 1,
        "project": 1,
        "concept": 1,
        "event": 0,
        "experience": 0,
        "conversation": 0,
    }
    assert payload["entities_by_tier"] == {"1": 1, "2": 1, "3": 0}
    assert payload["facts_active"] == 1
    assert payload["facts_superseded"] == 1
    assert payload["events_count"] == 2
    assert payload["pending_reviews"] == 1
    assert payload["last_ingest_at"] == "2026-04-27T10:00:00+00:00"
    assert payload["git_dirty"] is False
    assert payload["embedding_coverage"] == {
        "total_chunks": 6,
        "indexed_chunks": 2,
        "missing_chunks": 4,
        "ratio": 0.333333,
    }
    assert payload["last_reindex_at"] == "2026-04-27T11:00:00+00:00"
    assert payload["active_import_jobs"] == 2
    assert payload["token_usage"] == {
        "embedding": 1234,
        "extraction": 567,
        "total": 1801,
    }
    assert payload["total_cost_usd"] == 0.42
    assert isinstance(payload["facts_active"], int)
    assert isinstance(payload["git_dirty"], bool)


def test_status_json_last_ingest_can_be_null(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("brain.pipeline.status.git_ops.is_dirty", lambda _root: False)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["last_ingest_at"] is None


def test_status_phase2_defaults_when_tables_are_missing(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with connect(brain_root / "brain.db") as conn:
        conn.execute("DROP TABLE IF EXISTS import_files")
        conn.execute("DROP TABLE IF EXISTS import_jobs")
        conn.execute("DROP TABLE IF EXISTS embedding_index")
        conn.execute("DROP TABLE IF EXISTS stats")
        conn.commit()

    monkeypatch.setattr("brain.pipeline.status.git_ops.is_dirty", lambda _root: False)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["embedding_coverage"]["indexed_chunks"] == 0
    assert payload["last_reindex_at"] is None
    assert payload["active_import_jobs"] == 0
    assert payload["token_usage"] == {"embedding": 0, "extraction": 0, "total": 0}
    assert payload["total_cost_usd"] == 0.0


def test_status_missing_database_reports_stderr_exit_one(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (brain_root / "brain.db").unlink()
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    assert "Error: Required brain file not found:" in result.stderr


def test_status_invalid_root_reports_stderr_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    assert "Error: Required brain file not found:" in result.stderr


def test_main_app_wires_capture_and_entity_routes(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(brain_root)

    capture = runner.invoke(app, ["capture", "--stdin"], input="Status smoke capture\n")
    entity = runner.invoke(app, ["entity", "merge", "missing-a", "missing-b", "--yes"])

    assert capture.exit_code == 0
    assert "Capture summary:" in capture.stdout
    assert entity.exit_code == 1
    assert "Error:" in entity.stderr


def _seed_status_fixture(brain_root: Path) -> None:
    _write_page(brain_root, "entities/alice.md", PageType.ENTITY, tier=Tier.TIER_1)
    _write_page(brain_root, "projects/brain.md", PageType.PROJECT)
    _write_page(brain_root, "concepts/memory.md", PageType.CONCEPT)
    (brain_root / "events.jsonl").write_text("{}\n{}\n", encoding="utf-8", newline="\n")
    (brain_root / "review" / "pending.md").write_text(
        "---\nreview_id: pending\nkind: low_confidence_fact\nstatus: pending\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    _seed_db(brain_root)


def _seed_db(brain_root: Path) -> None:
    now = _utc().isoformat()
    with connect(brain_root / "brain.db") as conn:
        conn.execute(
            """
            INSERT INTO entities (id, type, title, page_path, tier, mention_count, first_seen, last_seen, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("alice", "person", "Alice", "pages/entities/alice.md", 1, 0, now, now, "{}"),
        )
        conn.execute(
            """
            INSERT INTO entities (id, type, title, page_path, tier, mention_count, first_seen, last_seen, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("brain", "project", "Brain", "pages/projects/brain.md", 2, 0, now, now, "{}"),
        )
        new_fact_id = conn.execute(
            """
            INSERT INTO facts (subject, predicate, object, object_type, asserted_at, source_event, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("alice", "role", "owner", "literal", now, "event-1", 0.9),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO facts (subject, predicate, object, object_type, asserted_at, source_event, confidence, superseded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("alice", "role", "maintainer", "literal", now, "event-2", 0.8, new_fact_id),
        )
        conn.execute(
            """
            INSERT INTO ingest_cursor (source, last_processed, last_run_at)
            VALUES (?, ?, ?)
            """,
            ("laundry", "item.md", "2026-04-27T10:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO embedding_index (
                rowid, page_slug, chunk_kind, chunk_id, content_hash, model, text_preview, created_at
            )
            VALUES
                (1, 'alice', 'compiled_truth', 'main', 'hash-1', 'model-a', 'truth', ?),
                (2, 'brain', 'compiled_truth', 'main', 'hash-2', 'model-a', 'truth', ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO import_jobs (id, source_path, started_at, status, total_files)
            VALUES
                ('job-running', 'notes', ?, 'running', 1),
                ('job-paused', 'notes', ?, 'paused', 1),
                ('job-completed', 'notes', ?, 'completed', 1)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            UPDATE stats
            SET value = CASE key
                WHEN 'last_reindex_at' THEN '2026-04-27T11:00:00+00:00'
                WHEN 'total_embedding_tokens' THEN '1234'
                WHEN 'total_extraction_tokens' THEN '567'
                WHEN 'total_cost_usd' THEN '0.42'
                ELSE value
            END
            WHERE key IN (
                'last_reindex_at',
                'total_embedding_tokens',
                'total_extraction_tokens',
                'total_cost_usd'
            )
            """
        )
        conn.commit()


def _write_page(
    brain_root: Path,
    relative: str,
    page_type: PageType,
    *,
    tier: Tier | None = None,
) -> None:
    slug = Path(relative).stem
    page = Page(
        frontmatter=Frontmatter(
            type=page_type,
            slug=slug,
            title=slug.title(),
            tier=tier,
            created=_utc(),
            updated=_utc(),
            tags=[],
            aliases=[],
            external_ids={},
        ),
        compiled_truth="truth",
        timeline=["- 2026-04-27 [event:01KQA8R9KVCG906A0203VYEQF7]: Created page"],
        sources=[],
    )
    write_page(brain_root / "pages" / relative, page)


def _utc() -> datetime:
    return datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
