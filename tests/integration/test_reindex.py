from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from typer.testing import CliRunner

from brain.cli.main import app
from brain.db.connection import connect
from brain.db.embeddings import find_embeddings_for_page
from brain.db.stats import get_stat
from brain.models import Frontmatter, Page, PageType, Tier
from brain.pages import parse_page, write_page
from brain.pages.writer import update_compiled_truth
from brain.pipeline.reindex import reindex

DIMENSION = 1536
EVENT_ONE = "01KQA8R9KVCG906A0203VYEQF7"
EVENT_TWO = "01KQA8VZMXBAV7AKF5JFB4KQ9C"
EVENT_THREE = "01KQA8X7QS3CRQ0H64K42Z14K2"


class FakeEmbeddingClient:
    calls: ClassVar[list[list[str]]] = []

    def __init__(self, _config) -> None:
        self.last_call_tokens = 0

    @classmethod
    def reset(cls) -> None:
        cls.calls = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        self.last_call_tokens = sum(max(1, len(text) // 4) for text in texts)
        return [[float(index + 1)] * DIMENSION for index, _ in enumerate(texts)]


def test_first_reindex_writes_chunks(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    _write_sample_page(brain_root, "alpha")
    _write_sample_page(brain_root, "beta")

    report = reindex(brain_root)

    assert report.chunks_added == 4
    assert report.chunks_updated == 0
    assert report.chunks_removed == 0
    assert report.chunks_unchanged == 0
    assert _embedding_count(brain_root) == 4
    assert _event_metadata(brain_root)["chunks_added"] == 4


def test_second_reindex_is_unchanged_without_embedding_call(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    _write_sample_page(brain_root, "alpha")
    first = reindex(brain_root)
    FakeEmbeddingClient.reset()

    second = reindex(brain_root)

    assert first.chunks_added == 2
    assert second.chunks_unchanged == 2
    assert second.chunks_added == 0
    assert second.chunks_updated == 0
    assert FakeEmbeddingClient.calls == []


def test_modified_page_updates_only_changed_chunk(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    page_path = _write_sample_page(brain_root, "alpha")
    reindex(brain_root)
    update_compiled_truth(page_path, "Alpha has a revised compiled truth.")
    FakeEmbeddingClient.reset()

    report = reindex(brain_root)

    assert report.chunks_added == 0
    assert report.chunks_updated == 1
    assert report.chunks_unchanged == 1
    assert _embedded_texts() == ["Alpha\n\nAlpha has a revised compiled truth."]


def test_deleted_timeline_entry_removes_orphan(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    page_path = _write_sample_page(brain_root, "alpha", timeline_count=2)
    reindex(brain_root)
    page = parse_page(page_path)
    page.timeline = page.timeline[:1]
    write_page(page_path, page)

    report = reindex(brain_root)

    assert report.chunks_removed == 1
    assert _embedding_count(brain_root) == 2


def test_deleted_page_file_removes_all_page_embeddings(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    page_path = _write_sample_page(brain_root, "alpha", timeline_count=2)
    reindex(brain_root)
    page_path.unlink()

    report = reindex(brain_root)

    assert report.chunks_removed == 3
    assert _records_for(brain_root, "alpha") == []
    assert _embedding_count(brain_root) == 0


def test_deleted_page_file_dry_run_reports_without_deleting(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    page_path = _write_sample_page(brain_root, "alpha", timeline_count=2)
    reindex(brain_root)
    page_path.unlink()
    before_events = (brain_root / "events.jsonl").read_text(encoding="utf-8")
    before_last_reindex = _stat(brain_root, "last_reindex_at")

    report = reindex(brain_root, dry_run=True)

    assert report.chunks_removed == 3
    assert len(_records_for(brain_root, "alpha")) == 3
    assert _embedding_count(brain_root) == 3
    assert (brain_root / "events.jsonl").read_text(encoding="utf-8") == before_events
    assert _stat(brain_root, "last_reindex_at") == before_last_reindex


def test_page_filter_cleans_explicitly_missing_slug_only(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    alpha_path = _write_sample_page(brain_root, "alpha")
    _write_sample_page(brain_root, "beta")
    reindex(brain_root)
    alpha_path.unlink()

    report = reindex(brain_root, page_filter="alpha")

    assert report.chunks_removed == 2
    assert _records_for(brain_root, "alpha") == []
    assert len(_records_for(brain_root, "beta")) == 2


def test_force_reembeds_unchanged_chunks(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    _write_sample_page(brain_root, "alpha")
    reindex(brain_root)
    FakeEmbeddingClient.reset()

    report = reindex(brain_root, force=True)

    assert report.chunks_updated == 2
    assert report.chunks_unchanged == 0
    assert len(_embedded_texts()) == 2


def test_page_filter_only_processes_requested_slug(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    _write_sample_page(brain_root, "alpha")
    _write_sample_page(brain_root, "beta")

    report = reindex(brain_root, page_filter="beta")

    assert report.pages_scanned == 1
    assert report.chunks_added == 2
    assert _records_for(brain_root, "alpha") == []
    assert len(_records_for(brain_root, "beta")) == 2


def test_dry_run_does_not_write_or_embed(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    _write_sample_page(brain_root, "alpha")
    before_events = (brain_root / "events.jsonl").read_text(encoding="utf-8")
    before_last_reindex = _stat(brain_root, "last_reindex_at")

    report = reindex(brain_root, dry_run=True)

    assert report.dry_run is True
    assert report.chunks_added == 2
    assert _embedding_count(brain_root) == 0
    assert (brain_root / "events.jsonl").read_text(encoding="utf-8") == before_events
    assert _stat(brain_root, "last_reindex_at") == before_last_reindex
    assert FakeEmbeddingClient.calls == []


def test_cli_reindex_command_is_registered(brain_root: Path, monkeypatch) -> None:
    _patch_embedding(monkeypatch)
    _write_sample_page(brain_root, "alpha")
    runner = CliRunner()

    result = runner.invoke(app, ["reindex", "--brain-root", str(brain_root)])

    assert result.exit_code == 0
    assert "Reindex summary:" in result.stdout
    assert "added=2" in result.stdout


def _patch_embedding(monkeypatch) -> None:
    FakeEmbeddingClient.reset()
    monkeypatch.setattr("brain.pipeline.reindex.OpenAICompatibleEmbeddingClient", FakeEmbeddingClient)


def _write_sample_page(root: Path, slug: str, timeline_count: int = 1) -> Path:
    page = Page(
        frontmatter=Frontmatter(
            type=PageType.ENTITY,
            slug=slug,
            title=slug.title(),
            tier=Tier.TIER_2,
            created=_now(),
            updated=_now(),
        ),
        compiled_truth=f"{slug.title()} compiled truth.",
        timeline=_timeline(slug, timeline_count),
        sources=[],
    )
    path = root / "pages" / "entities" / f"{slug}.md"
    write_page(path, page)
    return path


def _timeline(slug: str, count: int) -> list[str]:
    event_ids = [EVENT_ONE, EVENT_TWO, EVENT_THREE]
    return [
        f"- 2026-04-{index + 1:02d} [event:{event_ids[index]}]: {slug} event {index + 1}"
        for index in range(count)
    ]


def _now() -> datetime:
    return datetime(2026, 4, 30, 12, 0, tzinfo=UTC)


def _embedding_count(root: Path) -> int:
    with connect(root / "brain.db") as conn:
        return conn.execute("SELECT COUNT(*) FROM embedding_index").fetchone()[0]


def _records_for(root: Path, slug: str):
    with connect(root / "brain.db") as conn:
        return find_embeddings_for_page(conn, slug)


def _event_metadata(root: Path) -> dict[str, object]:
    lines = (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])["metadata"]


def _stat(root: Path, key: str) -> str | None:
    with connect(root / "brain.db") as conn:
        return get_stat(conn, key)


def _embedded_texts() -> list[str]:
    return [text for call in FakeEmbeddingClient.calls for text in call]
