from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.config import EmbeddingConfig
from brain.db.connection import connect
from brain.db.entities import upsert_entity
from brain.db.facts import add_fact
from brain.exceptions import BrainError, EmbeddingError, LLMError
from brain.import_ import importer
from brain.integrations.codex.install import (
    collect_integration_status,
    install_integration,
    uninstall_integration,
)
from brain.ledger import append_event
from brain.llm import client as llm_client
from brain.llm.embedding import OpenAICompatibleEmbeddingClient
from brain.mcp.http_config import HttpConfig
from brain.mcp.server_http import build_server
from brain.models import Entity, Event, Fact, FactCandidate, Frontmatter, Page
from brain.pages import parse_page, write_page
from brain.paths import BrainPaths
from brain.pipeline.ask import ask
from brain.pipeline.injection import inject
from brain.pipeline.rebuild import rebuild_db
from brain.pipeline.review import apply_pending
from brain.pipeline.signal_detect import SignalExtraction

NOW = datetime(2026, 9, 6, tzinfo=UTC)
EVENT_IDS = [f"01KQA8R9KVCG906A0203VYEQF{n}" for n in range(4)]


def seed_page(root: Path, slug: str = "alice", aliases: list[str] | None = None) -> Path:
    page_path = root / "pages" / "entities" / f"{slug}.md"
    with closing(connect(root / "brain.db")) as conn, conn:
        upsert_entity(conn, Entity(
            id=slug, type="person", title=slug.title(), tier=3,
            page_path=page_path.relative_to(root).as_posix(),
            first_seen=NOW, last_seen=NOW,
        ))
    write_page(page_path, Page(
        frontmatter=Frontmatter(
            type="entity", slug=slug, title=slug.title(), tier=3,
            created=NOW, updated=NOW, aliases=aliases or [],
        ),
        compiled_truth="Alice works at OldCo.",
        timeline=[f"- 2026-09-01 [event:{EVENT_IDS[0]}]: Alice works at OldCo."],
        sources=["laundry/old.md"],
    ))
    return page_path


def seed_events(root: Path) -> None:
    for index, event_id in enumerate(EVENT_IDS):
        append_event(root / "events.jsonl", Event(
            id=event_id, timestamp=NOW,
            kind="note_appended" if index % 2 == 0 else "reindexed",
            source_ref=f"event-{index}",
            raw_payload=f"note-{index}" if index % 2 == 0 else None,
        ))


@pytest.mark.parametrize("failure", ["missing_key", "provider", "apply"])
def test_event_retry_does_not_skip_unprocessed_payloads(brain_root, monkeypatch, failure):
    pipeline = importlib.import_module("brain.pipeline.ingest")
    seed_events(brain_root)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic")
    monkeypatch.setattr(pipeline, "_detect_item_signal", lambda item: SignalExtraction(timeline_summary="note"))
    if failure == "missing_key":
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    else:
        target = "_detect_item_signal" if failure == "provider" else "_apply_extraction"
        original = getattr(pipeline, target)

        def fail_second(*args, **kwargs):
            item = args[0] if args else kwargs["item"]
            if item.event.id == EVENT_IDS[2]:
                raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        monkeypatch.setattr(pipeline, target, fail_second)
    with pytest.raises(BrainError):
        pipeline.ingest(brain_root, source="events", auto_commit=False, auto_reindex=False)
    with closing(connect(brain_root / "brain.db")) as conn:
        pending = pipeline._collect_event_items(BrainPaths(brain_root), conn)
    expected = [EVENT_IDS[2]] if failure == "apply" else [EVENT_IDS[0], EVENT_IDS[2]]
    assert [item.event.id for item in pending] == expected


@pytest.mark.parametrize("limit", [0, 1])
def test_event_limit_preserves_remaining_payloads(brain_root, monkeypatch, limit):
    pipeline = importlib.import_module("brain.pipeline.ingest")
    seed_events(brain_root)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic")
    monkeypatch.setattr(pipeline, "_detect_item_signal", lambda item: SignalExtraction(timeline_summary="note"))
    pipeline.ingest(brain_root, source="events", limit=limit, auto_commit=False, auto_reindex=False)
    with closing(connect(brain_root / "brain.db")) as conn:
        pending = pipeline._collect_event_items(BrainPaths(brain_root), conn)
    assert [item.event.id for item in pending] == [EVENT_IDS[0], EVENT_IDS[2]][limit:]


def test_targeted_event_does_not_skip_earlier_events(brain_root, monkeypatch):
    pipeline = importlib.import_module("brain.pipeline.ingest")
    seed_events(brain_root)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic")
    monkeypatch.setattr(pipeline, "_detect_item_signal", lambda item: SignalExtraction(timeline_summary="note"))
    pipeline.ingest(brain_root, source="events", event_id=EVENT_IDS[2], auto_commit=False, auto_reindex=False)
    with closing(connect(brain_root / "brain.db")) as conn:
        assert pipeline._get_cursor(conn, "events") is None


@pytest.mark.parametrize("failure", ["alias", "parse", "index", "busy"])
def test_rebuild_failure_preserves_original_database(brain_root, monkeypatch, failure):
    pipeline = importlib.import_module("brain.pipeline.rebuild")
    page_path = seed_page(brain_root, aliases=["SharedAlias"])
    with closing(connect(brain_root / "brain.db")) as conn, conn:
        add_fact(conn, Fact(
            subject="alice", predicate="works_at", object="OldCo", object_type="literal",
            asserted_at=NOW, source_event=EVENT_IDS[0], confidence=0.9,
        ))
    if failure == "alias":
        seed_page(brain_root, "bob", aliases=["SharedAlias"])
    elif failure == "parse":
        page_path.write_text("invalid page", encoding="utf-8")
    elif failure == "index":
        def fail_index(root):
            raise OSError("synthetic write failure")
        monkeypatch.setattr(pipeline, "regenerate_index", fail_index)
    with closing(connect(brain_root / "brain.db")) as other_writer:
        if failure == "busy":
            other_writer.execute("BEGIN IMMEDIATE")
        with pytest.raises((BrainError, OSError)):
            rebuild_db(brain_root, auto_commit=False)
        other_writer.rollback()
    with closing(connect(brain_root / "brain.db")) as conn:
        assert conn.execute("SELECT object FROM facts").fetchone()[0] == "OldCo"
        assert conn.execute("SELECT title FROM entities WHERE id = 'alice'").fetchone()[0] == "Alice"


@pytest.mark.parametrize("kind", ["fact_conflict", "low_confidence_fact"])
@pytest.mark.parametrize("existing_page", [True, False])
def test_approved_fact_reaches_local_recall(brain_root, kind, existing_page):
    path = seed_page(brain_root)
    if not existing_page:
        path.unlink()
    candidate = FactCandidate(
        subject="alice", predicate="works_at", object="NewCo", object_type="literal",
        confidence=0.7, source_event=EVENT_IDS[2], source_ref="laundry/new.md",
    )
    review_path = brain_root / "review" / f"2026-09-06_001_{kind}.md"
    review_path.write_text(
        f"---\nkind: {kind}\nstatus: pending\n---\n\n```json\n"
        + candidate.model_dump_json() + "\n```\n\n## Decision\n\n[x] approve\n",
        encoding="utf-8",
    )
    report = apply_pending(brain_root)
    assert report.applied == 1, report.errors
    page = parse_page(path)
    assert "laundry/new.md" in page.sources
    assert any("NewCo" in line for line in page.timeline)
    assert ask(brain_root, "NewCo", mode="keyword-only").results
    assert "NewCo" in inject(brain_root, "Alice", include_snapshot=False).content


@pytest.mark.parametrize("key", [None, "", "   "])
def test_embedding_key_does_not_fall_back_to_openai(monkeypatch, key):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-synthetic-key")
    monkeypatch.delenv("PRIVATE_EMBEDDING_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("PRIVATE_EMBEDDING_KEY", key)
    def forbidden_client(**kwargs):
        pytest.fail("Missing configured key must fail before SDK construction")
    monkeypatch.setattr("openai.OpenAI", forbidden_client)
    with pytest.raises(EmbeddingError, match="PRIVATE_EMBEDDING_KEY"):
        OpenAICompatibleEmbeddingClient(EmbeddingConfig(
            api_key_env="PRIVATE_EMBEDDING_KEY", base_url="https://embedding.invalid/v1",
        ))


@pytest.mark.parametrize("provider", ["deepseek", "openai", "anthropic"])
@pytest.mark.parametrize("key", [None, "", "   "])
def test_llm_clients_require_selected_key(monkeypatch, provider, key):
    def forbidden_client(**kwargs):
        pytest.fail("Missing selected key must fail before SDK construction")
    monkeypatch.setattr("openai.OpenAI", forbidden_client)
    monkeypatch.setattr("anthropic.Anthropic", forbidden_client)
    settings = llm_client._LLMSettings(
        provider=provider, model="test", fast_model="test", api_key=key,
        base_url="https://provider.invalid/v1",
    )
    with pytest.raises(LLMError, match="API key is not set"):
        getattr(llm_client, f"_extract_{provider}")("synthetic", settings, use_fast=True)


def test_import_resume_keeps_source_and_cumulative_progress(brain_root, tmp_path, monkeypatch):
    source = tmp_path / "project-a" / "notes"
    other = tmp_path / "project-b" / "notes"
    source.mkdir(parents=True)
    other.mkdir(parents=True)
    (source / "first.md").write_text("First", encoding="utf-8")
    (source / "second.md").write_text("CorrectProjectA", encoding="utf-8")
    (other / "second.md").write_text("WrongProjectB", encoding="utf-8")
    original = importer._extract_documents
    def interrupt_second(path):
        if path.name == "second.md":
            raise KeyboardInterrupt
        return original(path)
    monkeypatch.chdir(source.parent)
    with monkeypatch.context() as patcher:
        patcher.setattr(importer, "_extract_documents", interrupt_second)
        with pytest.raises(KeyboardInterrupt):
            importer.import_path(brain_root, "notes", yes=True)
    monkeypatch.chdir(other.parent)
    report = importer.import_path(brain_root, resume=True, yes=True)
    assert report.status == "completed"
    body = "\n".join(path.read_text(encoding="utf-8") for path in (brain_root / "laundry").rglob("*.md"))
    assert "CorrectProjectA" in body and "WrongProjectB" not in body
    with closing(connect(brain_root / "brain.db")) as conn:
        assert tuple(conn.execute("SELECT processed_files, total_files FROM import_jobs").fetchone()) == (2, 2)


def test_import_resume_detects_changed_source(brain_root, tmp_path, monkeypatch):
    source = tmp_path / "note.md"
    source.write_text("Original", encoding="utf-8")
    with monkeypatch.context() as patcher:
        def interrupt(path):
            raise KeyboardInterrupt
        patcher.setattr(importer, "_extract_documents", interrupt)
        with pytest.raises(KeyboardInterrupt):
            importer.import_path(brain_root, source, yes=True)
    source.write_text("Changed", encoding="utf-8")
    report = importer.import_path(brain_root, resume=True, yes=True)
    assert report.failed == 1 and report.laundry == 0
    assert "changed since discovery" in report.errors[0]


def test_legacy_relative_import_job_fails_without_reading_current_directory(brain_root):
    with closing(connect(brain_root / "brain.db")) as conn, conn:
        conn.execute(
            "INSERT INTO import_jobs (id, source_path, started_at, status, total_files) "
            "VALUES ('legacy', 'notes', ?, 'paused', 1)", (NOW.isoformat(),),
        )
    with pytest.raises(BrainError, match="relative source path"):
        importer.import_path(brain_root, resume=True, yes=True)


def test_status_reports_stale_chunks_after_edit_without_calling_provider(brain_root, monkeypatch):
    from brain.config import load_config
    from brain.db.embeddings import upsert_embedding
    from brain.pipeline.chunking import embedding_content_hash, split_page_into_chunks
    from brain.pipeline.status import collect_status

    path = seed_page(brain_root)
    config = load_config(brain_root / "config.toml").embedding
    with closing(connect(brain_root / "brain.db")) as conn, conn:
        for chunk in split_page_into_chunks(parse_page(path), config.chunk_max_chars):
            upsert_embedding(conn, chunk, embedding_content_hash(
                chunk.text, model=config.model, dimension=config.dimension,
            ), [0.0] * config.dimension, config.model)
    before = collect_status(brain_root).embedding_coverage
    assert before["ratio"] == 1.0
    page = parse_page(path)
    page.compiled_truth = "Alice works at NewCo."
    write_page(path, page)
    after = collect_status(brain_root).embedding_coverage
    assert after["total_chunks"] == before["total_chunks"]
    assert after["indexed_chunks"] == before["indexed_chunks"]
    assert after["stale_chunks"] == 1 and after["ratio"] < 1.0
    assert collect_status(brain_root).to_dict()["cost_scope"] == "recorded_embedding_only"


def test_approval_with_missing_entity_remains_pending(brain_root):
    candidate = FactCandidate(
        subject="missing-entity", predicate="works_at", object="NewCo", object_type="literal",
        confidence=0.7, source_event=EVENT_IDS[2], source_ref="laundry/new.md",
    )
    path = brain_root / "review" / "2026-09-06_001_low_confidence_fact.md"
    path.write_text(
        "---\nkind: low_confidence_fact\nstatus: pending\n---\n\n```json\n"
        + candidate.model_dump_json() + "\n```\n\n## Decision\n\n[x] approve\n",
        encoding="utf-8",
    )
    report = apply_pending(brain_root)
    assert report.applied == 0 and path.exists()
    with closing(connect(brain_root / "brain.db")) as conn:
        assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 0


def test_scratch_injection_preserves_matched_body(brain_root):
    scratch = brain_root / "scratch" / "working.md"
    scratch.parent.mkdir(exist_ok=True)
    scratch.write_text("Old context. " * 90 + "\n\nLATEST_TARGET: Use port 9123.", encoding="utf-8")
    result = inject(brain_root, "LATEST_TARGET", include_snapshot=False, budget=10000)
    assert "Use port 9123" in result.content


@pytest.mark.parametrize("include_snapshot", [True, False])
def test_snapshot_is_included_at_most_once_and_respects_switch(brain_root, include_snapshot):
    snapshot = brain_root / "scratch" / "SNAPSHOT.md"
    snapshot.parent.mkdir(exist_ok=True)
    snapshot.write_text("SNAPSHOT_TARGET: unique snapshot body.", encoding="utf-8")
    result = inject(brain_root, "SNAPSHOT_TARGET", include_snapshot=include_snapshot)
    assert result.content.count("unique snapshot body") == int(include_snapshot)


@pytest.mark.parametrize("output_format", ["markdown", "text"])
def test_injection_uses_latest_timeline(brain_root, output_format):
    path = seed_page(brain_root)
    page = parse_page(path)
    page.timeline = [f"- 2026-09-0{day} [event:{EVENT_IDS[0]}]: Port {9100 + day}." for day in range(1, 6)]
    write_page(path, page)
    result = inject(brain_root, "Alice", include_snapshot=False, output_format=output_format)
    assert "Port 9105" in result.content and "Port 9101" not in result.content


def test_discoverable_skill_archive_is_detected_migrated_and_restorable(brain_root, tmp_path):
    codex, agents = tmp_path / "codex", tmp_path / "agents"
    legacy = codex / "skills" / "brain-memory.disabled-by-brainmem"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("legacy content", encoding="utf-8")
    args = dict(brain_root=brain_root, codex_home=codex, agents_home=agents)
    assert collect_integration_status(**args, check_commands=False)["legacy_duplicate_skill"]
    install_integration(**args, archive_legacy_skill=True, apply=True)
    assert not list((codex / "skills").rglob("SKILL.md"))
    assert collect_integration_status(**args, check_commands=False)["ready"]
    uninstall_integration(codex_home=codex, agents_home=agents, apply=True)
    assert (codex / "skills" / "brain-memory" / "SKILL.md").read_text(encoding="utf-8") == "legacy content"


def test_http_slow_tool_leaves_loop_responsive_and_overlaps_read_calls(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    def slow_status(brain_root=None):
        calls.append(brain_root)
        started.set()
        assert release.wait(3), "event loop did not get a chance to release the worker"
        return {"ok": True}
    slow_status.__name__ = "brain_status"
    monkeypatch.setattr("brain.mcp.tools.brain_status", slow_status)
    server = build_server(HttpConfig(brain_root=tmp_path, enabled_tools=frozenset({"brain_status"})))

    async def exercise():
        first = asyncio.create_task(server.call_tool("brain_status", {}))
        try:
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            assert started.is_set()
            second = asyncio.create_task(server.call_tool("brain_status", {}))
            for _ in range(200):
                if len(calls) == 2:
                    break
                await asyncio.sleep(0.005)
            assert len(calls) == 2 and not first.done()
        finally:
            release.set()
        results = await asyncio.gather(first, second)
        assert len(calls) == 2
        assert all(json.loads(result[0][0].text) == {"ok": True} for result in results)

    asyncio.run(exercise())
