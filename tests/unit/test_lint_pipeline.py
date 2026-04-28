from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.cli.init import init_brain
from brain.db.connection import connect
from brain.db.entities import upsert_entity
from brain.db.facts import add_fact
from brain.db.migrations import init_db
from brain.models import (
    Entity,
    EntityType,
    Fact,
    FactObjectType,
    Frontmatter,
    Page,
    PageType,
    Tier,
)
from brain.pages import write_page
from brain.paths import BrainPaths
from brain.pipeline.lint import (
    LintKind,
    lint_citations,
    lint_contradictions,
    lint_orphans,
    lint_stale,
    run_lint,
)

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "brain.db"
    init_db(db_path)
    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def test_lint_contradictions_finds_active_fact_conflict(conn: sqlite3.Connection) -> None:
    _insert_fact(conn, object_value="UK", source_event=VALID_ULID)
    _insert_fact(conn, object_value="Singapore", source_event=SECOND_ULID)
    _insert_fact(conn, predicate="hobby", object_value="reading", source_event=VALID_ULID)
    conn.commit()

    issues = lint_contradictions(conn)

    assert len(issues) == 1
    assert issues[0].kind is LintKind.CONTRADICTIONS
    assert issues[0].subject == "zihan"
    assert issues[0].predicate == "location"
    assert issues[0].details["objects"] == ["Singapore", "UK"]


def test_lint_stale_checks_only_tier_one_pages(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    paths = BrainPaths(root)
    _write_entity_page(
        paths.entities_dir / "old.md",
        slug="old",
        tier=Tier.TIER_1,
        timeline=["- 2026-01-01 [event:old]: old update"],
    )
    _write_entity_page(
        paths.entities_dir / "recent.md",
        slug="recent",
        tier=Tier.TIER_1,
        timeline=["- 2026-04-25 [event:recent]: recent update"],
    )
    _write_entity_page(
        paths.entities_dir / "tier-two.md",
        slug="tier-two",
        tier=Tier.TIER_2,
        timeline=["- 2026-01-01 [event:tier-two]: old update"],
    )

    issues = lint_stale(paths, stale_days=30)

    assert [issue.slug for issue in issues] == ["old"]
    assert issues[0].page == "pages/entities/old.md"


def test_lint_orphans_finds_missing_entity_and_missing_page(
    tmp_path: Path,
    conn: sqlite3.Connection,
) -> None:
    root = tmp_path / "brain"
    paths = BrainPaths(root)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    _write_entity_page(
        paths.entities_dir / "source.md",
        slug="source",
        tier=Tier.TIER_1,
        timeline=[
            "- 2026-04-28 [event:source]: Met [[missing]] and [[no-page]] and [[ok]]"
        ],
    )
    _write_entity_page(paths.entities_dir / "ok.md", slug="ok", tier=Tier.TIER_3)
    upsert_entity(
        conn,
        _entity("no-page", page_path="pages/entities/no-page.md", now=now),
    )
    upsert_entity(
        conn,
        _entity("ok", page_path="pages/entities/ok.md", now=now),
    )
    conn.commit()

    issues = lint_orphans(conn, paths)

    assert {(issue.slug, issue.details["reason"]) for issue in issues} == {
        ("missing", "missing_entity"),
        ("no-page", "missing_page"),
    }


def test_lint_citations_finds_truth_wikilinks_missing_from_timeline(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    paths = BrainPaths(root)
    _write_entity_page(
        paths.entities_dir / "alice.md",
        slug="alice",
        tier=Tier.TIER_1,
        compiled_truth="Alice works with [[supported]] and [[unsupported]].",
        timeline=["- 2026-04-28 [event:alice]: Alice met [[supported]]"],
    )

    issues = lint_citations(paths)

    assert len(issues) == 1
    assert issues[0].kind is LintKind.CITATIONS
    assert issues[0].details["missing_timeline_links"] == ["unsupported"]


def test_run_lint_no_issue_records_result_without_review(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    init_brain(root)

    report = run_lint(root, [LintKind.CITATIONS])

    rows = _rows(root, "SELECT kind, issue_count, report_file FROM lint_results")
    assert report.issue_count == 0
    assert report.review_files == []
    assert rows == [("citations", 0, "")]
    assert list((root / "review").glob("*.md")) == []


def _insert_fact(
    conn: sqlite3.Connection,
    *,
    predicate: str = "location",
    object_value: str,
    source_event: str,
) -> None:
    add_fact(
        conn,
        Fact(
            subject="zihan",
            predicate=predicate,
            object=object_value,
            object_type=FactObjectType.LITERAL,
            valid_from="2026-04-01",
            valid_to=None,
            asserted_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
            source_event=source_event,
            source_ref="events.jsonl",
            confidence=0.9,
        ),
    )


def _write_entity_page(
    path: Path,
    *,
    slug: str,
    tier: Tier,
    compiled_truth: str = "",
    timeline: list[str] | None = None,
) -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    write_page(
        path,
        Page(
            frontmatter=Frontmatter(
                type=PageType.ENTITY,
                slug=slug,
                title=slug.title(),
                tier=tier,
                created=now,
                updated=now,
                tags=[],
                aliases=[],
                external_ids={},
            ),
            compiled_truth=compiled_truth,
            timeline=timeline or [],
            sources=[],
        ),
    )


def _entity(slug: str, *, page_path: str, now: datetime) -> Entity:
    return Entity(
        id=slug,
        type=EntityType.PERSON,
        title=slug.title(),
        page_path=page_path,
        tier=Tier.TIER_3,
        mention_count=0,
        first_seen=now,
        last_seen=now,
        metadata={},
    )


def _rows(root: Path, sql: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(root / "brain.db") as connection:
        return [tuple(row) for row in connection.execute(sql).fetchall()]
