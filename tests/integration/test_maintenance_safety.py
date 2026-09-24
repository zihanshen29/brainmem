from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from brain.backup import create_backup, restore_backup, source_manifest, verify_backup
from brain.concurrency import root_lock
from brain.db.connection import connect
from brain.db.entities import upsert_entity
from brain.db.facts import add_fact
from brain.exceptions import BrainError
from brain.models import Entity, Fact, FactCandidate, Frontmatter, Page
from brain.pages import parse_page, write_page
from brain.pipeline.capture import capture
from brain.pipeline.ingest import ingest
from brain.pipeline.rebuild import rebuild_db
from brain.pipeline.reconcile import apply_reconcile, plan_reconcile
from brain.pipeline.review import apply_pending, parse_review_file
from brain.pipeline.signal_detect import SignalEntity, SignalExtraction, detect_signal
from brain.pipeline.summaries import propose_summary
from brain.privacy import external_allowed

NOW = datetime(2026, 9, 24, tzinfo=UTC)
EVENT = "01KQA8R9KVCG906A0203VYEQF7"


def seed(root: Path, *, summary="Hand edited summary.", privacy=None):
    path = root / "pages/projects/project.md"
    write_page(
        path,
        Page(
            frontmatter=Frontmatter(
                type="project",
                slug="project",
                title="示例项目",
                created=NOW,
                updated=NOW,
                privacy=privacy,
            ),
            compiled_truth=summary,
            timeline=[f"- 2026-09-24 [event:{EVENT}]: 项目使用 SQLite。"],
            sources=[],
        ),
    )
    with closing(connect(root / "brain.db")) as conn, conn:
        upsert_entity(
            conn,
            Entity(
                id="project",
                type="project",
                title="示例项目",
                page_path="pages/projects/project.md",
                mention_count=19,
                first_seen=NOW,
                last_seen=NOW,
            ),
        )
        add_fact(
            conn,
            Fact(
                subject="project",
                predicate="uses",
                object="SQLite",
                object_type="literal",
                asserted_at=NOW,
                source_event=EVENT,
                confidence=0.95,
            ),
        )
    return path


def extraction():
    return SignalExtraction(
        entities=[SignalEntity(name="示例项目", type="project", confidence=0.95)],
        facts=[
            FactCandidate(
                subject="示例项目",
                predicate="uses",
                object="Python",
                object_type="literal",
                source_event="runtime",
                confidence=0.95,
            )
        ],
        timeline_summary="项目使用 Python。",
    )


def fake_extraction(monkeypatch, implementation=None):
    pipeline = importlib.import_module("brain.pipeline.ingest")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic")
    monkeypatch.setattr(
        pipeline, "_detect_item_signal", implementation or (lambda item: extraction())
    )
    return pipeline


def test_rebuild_preserves_primary_records_and_registers_projects(brain_root):
    path = seed(brain_root)
    with closing(connect(brain_root / "brain.db")) as conn, conn:
        conn.execute(
            "UPDATE entities SET page_path = 'pages/concepts/wrong.md' WHERE id = 'project'"
        )
        conn.execute("INSERT INTO ingest_cursor VALUES ('events', ?, 'today')", (EVENT,))
    rebuild_db(brain_root, auto_commit=False)
    with closing(connect(brain_root / "brain.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
        assert conn.execute("SELECT last_processed FROM ingest_cursor").fetchone()[0] == EVENT
        assert conn.execute("SELECT mention_count FROM entities").fetchone()[0] == 19
        assert (
            conn.execute("SELECT page_path FROM entities").fetchone()[0]
            == path.relative_to(brain_root).as_posix()
        )


def test_backup_includes_ignored_raw_and_restores_without_overwrite(brain_root, tmp_path):
    seed(brain_root)
    raw = brain_root / "raw/private.txt"
    raw.write_text("local synthetic source", encoding="utf-8")
    os.utime(raw, ns=(1_700_000_000_123456700, 1_700_000_000_123456700))
    archive = tmp_path / "complete.zip"
    before = source_manifest(brain_root)
    assert create_backup(brain_root, archive)["verified"]
    assert source_manifest(brain_root) == before
    assert verify_backup(archive)["verified"]
    restored = tmp_path / "recovered"
    restore_backup(archive, restored)
    assert (restored / "raw/private.txt").read_text() == "local synthetic source"
    assert (restored / "raw/private.txt").stat().st_mtime_ns == raw.stat().st_mtime_ns
    with closing(connect(restored / "brain.db", read_only=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    with pytest.raises(BrainError, match="never overwritten"):
        restore_backup(archive, brain_root)


def test_reconcile_is_readonly_and_requires_fresh_backup(brain_root, tmp_path):
    path = seed(brain_root)
    with closing(connect(brain_root / "brain.db")) as conn, conn:
        conn.execute("UPDATE entities SET page_path = 'wrong' WHERE id='project'")
    before = source_manifest(brain_root)
    plan = plan_reconcile(brain_root)
    assert source_manifest(brain_root) == before
    assert plan["counts"]["registry_changes"] == 1
    archive = tmp_path / "pre-migration.zip"
    create_backup(brain_root, archive)
    assert apply_reconcile(brain_root, plan, archive)["applied"]
    assert path.read_text(encoding="utf-8").find("Hand edited summary.") >= 0
    with pytest.raises(BrainError, match="stale"):
        apply_reconcile(brain_root, plan, archive)


def test_summary_is_preview_and_manual_edits_invalidate_draft(brain_root):
    path = seed(brain_root)
    original = path.read_bytes()
    result = propose_summary(brain_root, "project")
    assert path.read_bytes() == original
    review = brain_root / result["review_file"]
    assert "SQLite" in parse_review_file(review).data["compiled_truth"]
    review.write_text(
        review.read_text(encoding="utf-8").replace("[ ] approve", "[x] approve"), encoding="utf-8"
    )
    path.write_bytes(original.replace(b"Hand edited", b"New human"))
    report = apply_pending(brain_root)
    assert not report.applied and "changed since" in " ".join(report.errors)
    assert b"New human" in path.read_bytes()


def test_summary_approval_only_changes_requested_page(brain_root):
    path = seed(brain_root)
    result = propose_summary(brain_root, "project")
    review = brain_root / result["review_file"]
    review.write_text(
        review.read_text(encoding="utf-8").replace("[ ] approve", "[x] approve"), encoding="utf-8"
    )
    report = apply_pending(brain_root)
    assert report.approved == 1, report.errors
    assert "SQLite" in parse_page(path).compiled_truth


def test_private_notes_never_reach_detector_and_remain_pending(brain_root, monkeypatch):
    note = brain_root / "laundry/private.md"
    note.write_text("privacy: local-only\n\nsecret project note", encoding="utf-8")
    fake_extraction(monkeypatch, lambda item: pytest.fail("private content reached provider"))
    report = ingest(brain_root, source="laundry", auto_commit=False, auto_reindex=False)
    assert report.skipped_private == 1 and note.is_file()
    assert not list((brain_root / "review").glob("*.md"))


def test_page_privacy_inherits_source_and_path_policy(brain_root):
    path = seed(brain_root)
    source = brain_root / "raw/private.md"
    source.write_text("---\nprivacy: local-only\n---\nprivate text", encoding="utf-8")
    page = parse_page(path)
    page.sources.append("raw/private.md")
    write_page(path, page)
    assert not external_allowed(brain_root, path=path)
    with pytest.raises(BrainError, match="local-only"):
        propose_summary(brain_root, "project", provider=True)


def test_ingest_keeps_manual_summary_and_uses_archived_source(brain_root, monkeypatch):
    path = seed(brain_root)
    fake_extraction(monkeypatch)
    note = brain_root / "laundry/subdir/note.md"
    note.parent.mkdir()
    note.write_text(
        "source_agent: codex\nsource_context: test\n\n示例项目使用 Python。", encoding="utf-8"
    )
    result = ingest(brain_root, source="laundry", auto_commit=False, auto_reindex=False)
    assert result.facts_added == 1
    page = parse_page(path)
    assert page.compiled_truth == "Hand edited summary."
    assert "laundry/processed/subdir/note.md" in page.sources
    assert (brain_root / page.sources[0]).is_file()
    assert "source_agent" not in " ".join(page.timeline)


def test_cached_paid_extraction_survives_transient_batch_failure(brain_root, monkeypatch):
    seed(brain_root)
    calls = []

    def detect(item):
        calls.append(item.laundry_path.name)
        if item.laundry_path.name == "b.md":
            raise TimeoutError("synthetic temporary failure")
        return extraction()

    fake_extraction(monkeypatch, detect)
    for name in ("a.md", "b.md"):
        (brain_root / "laundry" / name).write_text("示例项目使用 Python。", encoding="utf-8")
    with pytest.raises(BrainError):
        ingest(brain_root, source="laundry", auto_commit=False, auto_reindex=False)

    def retry(item):
        calls.append("retry:" + item.laundry_path.name)
        return extraction()

    fake_extraction(monkeypatch, retry)
    ingest(brain_root, source="laundry", auto_commit=False, auto_reindex=False)
    assert calls == ["a.md", "b.md", "retry:b.md"]


def test_apply_failure_rolls_back_files_counts_and_database(brain_root, monkeypatch):
    seed(brain_root)
    pipeline = fake_extraction(monkeypatch)
    note = brain_root / "laundry/note.md"
    note.write_text("示例项目使用 Python。", encoding="utf-8")
    pages = {p: p.read_bytes() for p in (brain_root / "pages").rglob("*.md")}
    monkeypatch.setattr(
        pipeline,
        "_record_item_success",
        lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic")),
    )
    with pytest.raises(BrainError):
        ingest(brain_root, source="laundry", auto_commit=False, auto_reindex=False)
    assert {p: p.read_bytes() for p in (brain_root / "pages").rglob("*.md")} == pages
    with closing(connect(brain_root / "brain.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
        assert conn.execute("SELECT mention_count FROM entities").fetchone()[0] == 19
    assert note.exists()


def test_capture_can_run_during_slow_extraction(brain_root, monkeypatch):
    seed(brain_root)
    (brain_root / "laundry/note.md").write_text("示例项目使用 Python。", encoding="utf-8")
    started, release = threading.Event(), threading.Event()
    failures = []

    def detect(item):
        started.set()
        assert release.wait(8)
        return extraction()

    fake_extraction(monkeypatch, detect)

    def work():
        try:
            ingest(brain_root, source="laundry", auto_commit=False, auto_reindex=False)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert started.wait(5)
        assert capture(brain_root, "independent note", auto_commit=False).path
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not failures


def test_process_crash_recovers_only_touched_files(brain_root):
    path = seed(brain_root)
    original = path.read_bytes()
    script = """
import os, sys
from pathlib import Path
from brain.concurrency import root_lock
from brain.db.connection import connect
from brain.transactions import durable_unit, atomic_text
root = Path(sys.argv[1])
with root_lock(root, write=True):
    conn = connect(root / 'brain.db')
    with durable_unit(root, conn, 'crash-test'):
        atomic_text(root / 'pages/projects/project.md', 'interrupted')
        conn.execute("UPDATE entities SET mention_count=999")
        os._exit(9)
"""
    result = subprocess.run([sys.executable, "-c", script, str(brain_root)], timeout=20)
    assert result.returncode == 9
    untouched = brain_root / "pages/human-new.txt"
    untouched.write_text("new unrelated user file")
    with pytest.raises(BrainError, match="recovery"), root_lock(brain_root):
        pass
    with root_lock(brain_root, write=True):
        pass
    assert path.read_bytes() == original and untouched.read_text() == "new unrelated user file"
    with closing(connect(brain_root / "brain.db")) as conn:
        assert conn.execute("SELECT mention_count FROM entities").fetchone()[0] == 19


def test_lock_identity_does_not_depend_on_temp_or_windows_user(brain_root, monkeypatch, tmp_path):
    monkeypatch.delenv("BRAINMEM_LOCK_DIR", raising=False)
    script = """
import sys
from pathlib import Path
from brain.concurrency import root_lock, RootBusyError
try:
    with root_lock(Path(sys.argv[1]), write=True, timeout=.1):
        sys.exit(4)
except RootBusyError:
    sys.exit(0)
"""
    env = dict(
        os.environ, TEMP=str(tmp_path / "other-user-temp"), TMP=str(tmp_path / "other-user-temp")
    )
    with root_lock(brain_root, write=True):
        result = subprocess.run(
            [sys.executable, "-c", script, str(brain_root)], env=env, timeout=20
        )
    assert result.returncode == 0


def test_truncation_splits_input_and_preserves_entity_name(monkeypatch):
    from brain.llm import client

    calls = []

    def extract(text):
        calls.append(len(text))
        if len(text) > 500:
            raise client.TruncatedResponseError("truncated")
        return extraction()

    monkeypatch.setattr(client, "extract_signal", extract)
    result = detect_signal("中文事实。" * 180)
    assert len(calls) == 3 and result.entities[0].name == "示例项目"
    assert len(result.facts) == 1


@pytest.mark.parametrize("provider", ["deepseek", "openai", "anthropic"])
def test_providers_report_truncation_without_retry(monkeypatch, provider):
    from brain.llm import client

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="incomplete",
            stop_reason="max_tokens",
            output_text="{",
            choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="{"))],
        )

    def fake(**kwargs):
        return SimpleNamespace(
            responses=SimpleNamespace(create=create),
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            messages=SimpleNamespace(create=create),
        )

    monkeypatch.setattr("openai.OpenAI", fake)
    monkeypatch.setattr("anthropic.Anthropic", fake)
    settings = client._LLMSettings(
        provider=provider, model="mock", fast_model="mock", api_key="mock"
    )
    monkeypatch.setattr(client, "_resolve_llm_settings", lambda: settings)
    with pytest.raises(client.TruncatedResponseError):
        client._request_structured_json("synthetic")
    assert len(calls) == 1


def test_nested_mcp_capture_keeps_restrictive_privacy(brain_root, monkeypatch):
    from brain.mcp.tools import brain_capture

    result = brain_capture(
        "---\nprivacy: local-only\n---\n\nPrivate synthetic note.",
        source_agent="test",
        source_context="offline fixture",
        brain_root=brain_root,
    )
    path = brain_root / result["path"]
    assert not external_allowed(brain_root, path=path)
    fake_extraction(
        monkeypatch, lambda item: pytest.fail("nested private content reached provider")
    )
    report = ingest(brain_root, source="laundry", auto_commit=False, auto_reindex=False)
    assert report.skipped_private == 1 and path.exists()


def test_event_source_privacy_propagates_to_pages(brain_root):
    from brain.ledger import append_event
    from brain.models import Event

    path = seed(brain_root)
    append_event(
        brain_root / "events.jsonl",
        Event(
            id=EVENT,
            timestamp=NOW,
            kind="ai_chat",
            source_ref="fixture",
            raw_payload="Synthetic private text",
            metadata={"privacy": "local-only"},
        ),
    )
    page = parse_page(path)
    page.sources = [f"events.jsonl:{EVENT}"]
    write_page(path, page)
    assert not external_allowed(brain_root, path=path)


def test_explain_and_reindex_never_send_private_pages(brain_root, monkeypatch):
    from brain.pipeline.ask import ask
    from brain.pipeline.reindex import reindex

    path = seed(brain_root, privacy="local-only")
    monkeypatch.setattr(
        "brain.llm.client.answer_question", lambda *a, **k: pytest.fail("private explanation")
    )
    monkeypatch.setattr(
        "brain.pipeline.reindex.OpenAICompatibleEmbeddingClient",
        lambda *a, **k: pytest.fail("private embedding"),
    )
    result = ask(brain_root, "示例项目", mode="keyword-only", explain=True)
    assert result.results and result.answer is None
    before = (brain_root / "events.jsonl").read_bytes()
    report = reindex(brain_root)
    assert report.pages_private == 1 and not report.would_embed
    assert (brain_root / "events.jsonl").read_bytes() == before
    assert path.exists()


def test_bad_utf8_isolated_from_neighbor_note(brain_root, monkeypatch):
    (brain_root / "laundry/a-bad.md").write_bytes(b"\xff\xfe\x01")
    (brain_root / "laundry/b-good.md").write_text("Synthetic valid note.", encoding="utf-8")
    fake_extraction(monkeypatch)
    report = ingest(brain_root, source="laundry", auto_commit=False, auto_reindex=False)
    assert report.processed == 1 and len(report.errors) == 1
    assert (brain_root / "laundry/failed/a-bad.md").exists()
    assert (brain_root / "laundry/processed/b-good.md").exists()
    error = next((brain_root / "review").glob("*ingest_error.md"))
    assert not parse_review_file(error).errors


def test_source_edit_during_extraction_is_not_applied(brain_root, monkeypatch):
    seed(brain_root)
    note = brain_root / "laundry/note.md"
    note.write_text("original", encoding="utf-8")

    def extract(item):
        note.write_text("user changed this", encoding="utf-8")
        return extraction()

    fake_extraction(monkeypatch, extract)
    with pytest.raises(BrainError, match="Source changed"):
        ingest(brain_root, source="laundry", auto_commit=False, auto_reindex=False)
    assert note.read_text() == "user changed this"
    with closing(connect(brain_root / "brain.db", read_only=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


def test_reconcile_restores_aliases_without_registry_change(brain_root, tmp_path):
    path = seed(brain_root)
    page = parse_page(path)
    page.frontmatter.aliases = ["项目别名"]
    write_page(path, page)
    plan = plan_reconcile(brain_root)
    assert not plan["registry"] and plan["counts"]["aliases_added"] == 1
    archive = tmp_path / "alias-before.zip"
    create_backup(brain_root, archive)
    apply_reconcile(brain_root, plan, archive)
    with closing(connect(brain_root / "brain.db", read_only=True)) as conn:
        assert (
            conn.execute("SELECT entity_id FROM entity_aliases WHERE alias='项目别名'").fetchone()[
                0
            ]
            == "project"
        )


def test_rebuild_refuses_corruption_without_replacing_original(brain_root):
    path = brain_root / "brain.db"
    path.write_bytes(b"damaged synthetic database")
    with pytest.raises(BrainError, match=r"damaged|Could not connect"):
        rebuild_db(brain_root, auto_commit=False)
    assert path.read_bytes() == b"damaged synthetic database"


def test_readonly_live_wal_snapshot_preserves_source_bytes(brain_root):
    import hashlib

    path = brain_root / "brain.db"
    with closing(connect(path)) as writer:
        writer.execute("CREATE TABLE wal_fixture (value TEXT)")
        writer.execute("INSERT INTO wal_fixture VALUES ('committed WAL record')")
        writer.commit()
        paths = [path, path.with_name("brain.db-wal"), path.with_name("brain.db-shm")]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        with root_lock(brain_root), closing(connect(path, read_only=True)) as reader:
            assert (
                reader.execute("SELECT value FROM wal_fixture").fetchone()[0]
                == "committed WAL record"
            )
            with pytest.raises(Exception, match="readonly"):
                reader.execute("DELETE FROM wal_fixture")
        assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == before


def test_known_predicate_aliases_detect_conflicts_but_multivalue_does_not(brain_root):
    from brain.pipeline.conflict import Decision, classify_fact
    from brain.pipeline.lint import lint_contradictions

    seed(brain_root)
    with closing(connect(brain_root / "brain.db")) as conn, conn:
        add_fact(
            conn,
            Fact(
                subject="project",
                predicate="has current commit",
                object="aaa",
                object_type="literal",
                asserted_at=NOW,
                source_event=EVENT,
                confidence=0.95,
            ),
        )
        candidate = FactCandidate(
            subject="project",
            predicate="has_production_commit",
            object="bbb",
            object_type="literal",
            source_event=EVENT,
            confidence=0.95,
        )
        assert classify_fact(conn, candidate) is Decision.CONFLICT
        assert (
            classify_fact(
                conn, candidate.model_copy(update={"predicate": "uses", "object": "Python"})
            )
            is Decision.ADD
        )
        add_fact(conn, Fact(**candidate.model_dump(), asserted_at=NOW))
        assert len(lint_contradictions(conn)) == 1


def test_rejected_tier_has_evidence_growth_cooldown(brain_root):
    from brain.db.entities import get_entity
    from brain.pipeline.tier import check_tier_upgrade

    seed(brain_root)
    with closing(connect(brain_root / "brain.db")) as conn, conn:
        entity = get_entity(conn, "project")
        entity.metadata["tier_rejected_at_count"] = 19
        upsert_entity(conn, entity)
        assert check_tier_upgrade(conn, "project") is None
        conn.execute("UPDATE entities SET mention_count = 29 WHERE id='project'")
        assert check_tier_upgrade(conn, "project") is not None


def test_review_sequence_includes_archived_ids(brain_root):
    from brain.paths import BrainPaths
    from brain.pipeline.ingest import IngestReport, ReviewWriter

    writer = ReviewWriter.create(BrainPaths(brain_root), IngestReport())
    first = brain_root / writer.write("new_entity_review", "synthetic")
    archive = brain_root / "review/archive"
    archive.mkdir(exist_ok=True)
    first.replace(archive / first.name)
    second = ReviewWriter.create(BrainPaths(brain_root), IngestReport()).write(
        "new_entity_review", "synthetic"
    )
    assert Path(second).name != first.name
