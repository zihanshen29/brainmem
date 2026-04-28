from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import brain.pipeline.rebuild as rebuild_pipeline
from brain.cli.main import app
from brain.db.backlinks import replace_backlinks_for_page
from brain.db.connection import connect
from brain.models import (
    Backlink,
    Frontmatter,
    Page,
    PageType,
    Tier,
)
from brain.pages import parse_page, write_page

runner = CliRunner()
VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"


def test_cli_rebuild_db_recreates_queryable_entities_aliases_backlinks_and_summary(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(
        brain_root,
        "entities/alice.md",
        _page(slug="alice", title="Alice", tags=["person"], aliases=["Al"]),
    )
    _write_page(
        brain_root,
        "projects/brain.md",
        _page(
            slug="brain-project",
            title="Brain Project",
            page_type=PageType.PROJECT,
            compiled_truth="Al works on [[alice]].",
        ),
    )
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["rebuild", "--db", "--yes"])

    assert result.exit_code == 0
    _assert_summary_fields(result.stdout, "db")

    entities = _rows(brain_root, "SELECT id, type, title FROM entities")
    aliases = _rows(brain_root, "SELECT alias, entity_id, source FROM entity_aliases")
    backlinks = _rows(
        brain_root,
        "SELECT from_page, to_entity, relation FROM backlinks ORDER BY relation",
    )
    facts = _scalar(brain_root, "SELECT COUNT(*) FROM facts")

    assert [tuple(row) for row in entities] == [("alice", "person", "Alice")]
    assert [tuple(row) for row in aliases] == [("Al", "alice", "frontmatter")]
    assert ("brain-project", "alice", "mentions") in [tuple(row) for row in backlinks]
    assert ("brain-project", "alice", "works_with") in [tuple(row) for row in backlinks]
    assert facts == 0


def test_cli_rebuild_backlinks_replaces_stale_rows(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_rebuild_fixture_pages(brain_root)
    rebuild_pipeline.rebuild_db(brain_root, auto_commit=False)
    with connect(brain_root / "brain.db") as conn:
        replace_backlinks_for_page(
            conn,
            "brain-project",
            [
                Backlink(
                    from_page="brain-project",
                    to_entity="alice",
                    relation="stale",
                    line_number=1,
                    extracted_at=_utc_now(),
                )
            ],
        )
        conn.commit()
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["rebuild", "--backlinks", "--yes"])

    assert result.exit_code == 0
    _assert_summary_fields(result.stdout, "backlinks")
    links = _rows(brain_root, "SELECT relation FROM backlinks ORDER BY relation")
    relations = [row[0] for row in links]
    assert "stale" not in relations
    assert "mentions" in relations
    assert "works_with" in relations


def test_cli_rebuild_index_updates_pages_index(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(brain_root, "entities/alice.md", _page(slug="alice", title="Alice"))
    index_path = brain_root / "pages" / "index.md"
    index_path.write_text("stale\n", encoding="utf-8", newline="\n")
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["rebuild", "--index"])

    assert result.exit_code == 0
    _assert_summary_fields(result.stdout, "index")
    assert "- [Alice](entities/alice.md)" in index_path.read_text(encoding="utf-8")


def test_cli_rebuild_pages_force_rewrites_compiled_truth_without_real_llm(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_path = _write_page(
        brain_root,
        "entities/alice.md",
        _page(slug="alice", title="Alice", tags=["person"], aliases=["Al"]),
    )
    rebuild_pipeline.rebuild_db(brain_root, auto_commit=False)
    calls: list[tuple[int, str | None]] = []

    def fake_rewrite(timeline: list[Any], current_truth: str | None) -> str:
        calls.append((len(timeline), current_truth))
        return "updated compiled truth"

    monkeypatch.setattr(rebuild_pipeline.llm_client, "rewrite_compiled_truth", fake_rewrite)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["rebuild", "--pages", "alice", "--force", "--yes"])

    assert result.exit_code == 0
    _assert_summary_fields(result.stdout, "pages")
    assert calls == [(1, "old truth")]
    assert parse_page(page_path).compiled_truth == "updated compiled truth"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["rebuild"], "select exactly one rebuild scope"),
        (["rebuild", "--db", "--index"], "select exactly one rebuild scope"),
        (["rebuild", "--pages", "alice"], "--pages requires --force"),
    ],
)
def test_cli_rebuild_argument_errors_exit_one_and_write_stderr(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    message: str,
) -> None:
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert message in result.stderr


def test_cli_rebuild_user_rejects_confirmation_exits_one(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(brain_root, "entities/alice.md", _page(slug="alice", title="Alice"))
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["rebuild", "--db"], input="n\n")

    assert result.exit_code == 1
    assert "rebuild cancelled" in result.stderr


def _write_rebuild_fixture_pages(brain_root: Path) -> None:
    _write_page(
        brain_root,
        "entities/alice.md",
        _page(slug="alice", title="Alice", tags=["person"], aliases=["Al"]),
    )
    _write_page(
        brain_root,
        "projects/brain.md",
        _page(
            slug="brain-project",
            title="Brain Project",
            page_type=PageType.PROJECT,
            compiled_truth="Al works on [[alice]].",
        ),
    )


def _page(
    *,
    slug: str,
    title: str,
    page_type: PageType = PageType.ENTITY,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    compiled_truth: str = "old truth",
) -> Page:
    return Page(
        frontmatter=Frontmatter(
            type=page_type,
            slug=slug,
            title=title,
            tier=Tier.TIER_2 if page_type is PageType.ENTITY else None,
            created=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            updated=datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
            tags=tags or [],
            aliases=aliases or [],
            external_ids={},
        ),
        compiled_truth=compiled_truth,
        timeline=[f"- 2026-04-28 [event:{VALID_ULID}]: Created page"],
        sources=["events.jsonl"],
    )


def _write_page(brain_root: Path, relative: str, page: Page) -> Path:
    path = brain_root / "pages" / relative
    write_page(path, page)
    return path


def _rows(brain_root: Path, sql: str) -> list[sqlite3.Row]:
    with connect(brain_root / "brain.db") as conn:
        return conn.execute(sql).fetchall()


def _scalar(brain_root: Path, sql: str) -> Any:
    return _rows(brain_root, sql)[0][0]


def _assert_summary_fields(output: str, scope: str) -> None:
    assert "Rebuild summary:" in output
    for field in (
        f"scope={scope}",
        "pages_scanned=",
        "pages_touched=",
        "entities_rebuilt=",
        "aliases_rebuilt=",
        "backlinks_rebuilt=",
        "index_rebuilt=",
        "facts_rebuilt=",
        "committed=",
    ):
        assert field in output


def _utc_now() -> datetime:
    return datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
