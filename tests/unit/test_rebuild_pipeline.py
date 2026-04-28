import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import brain.pipeline.rebuild as rebuild_pipeline
from brain.db.backlinks import replace_backlinks_for_page
from brain.db.connection import connect
from brain.db.entities import add_alias, upsert_entity
from brain.db.migrations import init_db
from brain.exceptions import BrainError
from brain.models import (
    Backlink,
    Entity,
    EntityAliasSource,
    EntityType,
    Frontmatter,
    Page,
    PageType,
    Tier,
)
from brain.pages import parse_page, write_page
from brain.pipeline.rebuild import (
    rebuild_backlinks,
    rebuild_db,
    rebuild_index,
    rebuild_pages,
)

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"


def utc_datetime(day: int = 28) -> datetime:
    return datetime(2026, 4, day, 12, 0, tzinfo=UTC)


def make_page(
    *,
    slug: str,
    title: str,
    page_type: PageType = PageType.ENTITY,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    compiled_truth: str = "old truth",
    timeline: list[str] | None = None,
) -> Page:
    return Page(
        frontmatter=Frontmatter(
            type=page_type,
            slug=slug,
            title=title,
            tier=Tier.TIER_2 if page_type is PageType.ENTITY else None,
            created=utc_datetime(1),
            updated=utc_datetime(2),
            tags=tags or [],
            aliases=aliases or [],
            external_ids={},
        ),
        compiled_truth=compiled_truth,
        timeline=timeline or [f"- 2026-04-28 [event:{VALID_ULID}]: Created page"],
        sources=["events.jsonl"],
    )


def write_brain_page(brain_root: Path, relative: str, page: Page) -> Path:
    path = brain_root / "pages" / relative
    write_page(path, page)
    return path


def rows(brain_root: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = connect(brain_root / "brain.db")
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def sample_entity(entity_id: str, title: str, entity_type: EntityType) -> Entity:
    return Entity(
        id=entity_id,
        type=entity_type,
        title=title,
        page_path=f"pages/entities/{entity_id}.md",
        tier=Tier.TIER_3,
        mention_count=0,
        first_seen=utc_datetime(1),
        last_seen=utc_datetime(2),
    )


def test_rebuild_db_recreates_schema_entities_aliases_backlinks_without_facts(
    tmp_path: Path,
) -> None:
    brain_root = tmp_path / "brain"
    write_brain_page(
        brain_root,
        "entities/alice.md",
        make_page(slug="alice", title="Alice", tags=["person"], aliases=["Al"]),
    )
    write_brain_page(
        brain_root,
        "projects/brain.md",
        make_page(
            slug="brain-project",
            title="Brain Project",
            page_type=PageType.PROJECT,
            compiled_truth="Al works on [[alice]].",
        ),
    )
    (brain_root / "brain.db").parent.mkdir(parents=True, exist_ok=True)
    (brain_root / "brain.db").write_bytes(b"not sqlite")

    report = rebuild_db(brain_root, auto_commit=False)

    assert report.scope == "db"
    assert report.entities_rebuilt == 1
    assert report.aliases_rebuilt == 1
    assert report.backlinks_rebuilt >= 1
    assert report.facts_rebuilt == 0
    assert report.index_rebuilt is True
    assert not (brain_root / "brain.db-wal").exists()
    assert not (brain_root / "brain.db-shm").exists()

    entity = rows(brain_root, "SELECT * FROM entities WHERE id = ?", ("alice",))[0]
    aliases = rows(brain_root, "SELECT alias, entity_id, source FROM entity_aliases")
    backlinks = rows(
        brain_root,
        """
        SELECT from_page, to_entity, relation, line_number, extracted_at
        FROM backlinks
        """,
    )
    facts_count = rows(brain_root, "SELECT COUNT(*) AS count FROM facts")[0]["count"]

    assert entity["type"] == "person"
    assert entity["page_path"] == "pages/entities/alice.md"
    assert [tuple(row) for row in aliases] == [("Al", "alice", "frontmatter")]
    assert ("brain-project", "alice", "mentions", 14, utc_datetime(2).isoformat()) in [
        tuple(row) for row in backlinks
    ]
    assert facts_count == 0


def test_rebuild_db_backlink_rows_are_deterministic_across_runs(tmp_path: Path) -> None:
    brain_root = tmp_path / "brain"
    write_brain_page(
        brain_root,
        "entities/alice.md",
        make_page(slug="alice", title="Alice", tags=["person"], aliases=["Al"]),
    )
    write_brain_page(
        brain_root,
        "projects/brain.md",
        make_page(
            slug="brain-project",
            title="Brain Project",
            page_type=PageType.PROJECT,
            compiled_truth="Al works on [[alice]].",
        ),
    )

    rebuild_db(brain_root, auto_commit=False)
    first_rows = backlink_rows(brain_root)
    rebuild_db(brain_root, auto_commit=False)
    second_rows = backlink_rows(brain_root)

    assert first_rows == second_rows
    assert {row[4] for row in first_rows} == {utc_datetime(2).isoformat()}


def test_rebuild_db_alias_conflict_raises_brain_error(tmp_path: Path) -> None:
    brain_root = tmp_path / "brain"
    write_brain_page(
        brain_root,
        "entities/alice.md",
        make_page(slug="alice", title="Alice", aliases=["Shared"]),
    )
    write_brain_page(
        brain_root,
        "entities/bob.md",
        make_page(slug="bob", title="Bob", aliases=["Shared"]),
    )

    with pytest.raises(BrainError, match="points to multiple entities"):
        rebuild_db(brain_root, auto_commit=False)


def test_rebuild_backlinks_clears_stale_links_and_rebuilds_current_markdown(
    tmp_path: Path,
) -> None:
    brain_root = tmp_path / "brain"
    brain_root.mkdir()
    init_db(brain_root / "brain.db")
    write_brain_page(
        brain_root,
        "projects/brain.md",
        make_page(
            slug="brain-project",
            title="Brain Project",
            page_type=PageType.PROJECT,
            compiled_truth="Alice now owns retrieval.",
        ),
    )
    with connect(brain_root / "brain.db") as conn:
        upsert_entity(conn, sample_entity("alice", "Alice", EntityType.PERSON))
        upsert_entity(conn, sample_entity("old", "Old", EntityType.CONCEPT))
        add_alias(conn, "retrieval", "old", EntityAliasSource.MANUAL)
        replace_backlinks_for_page(
            conn,
            "brain-project",
            [
                Backlink(
                    from_page="brain-project",
                    to_entity="old",
                    relation="mentions",
                    line_number=1,
                    extracted_at=utc_datetime(),
                )
            ],
        )
        conn.commit()
        conn.execute(
            "UPDATE entity_aliases SET entity_id = ? WHERE alias = ?",
            ("alice", "retrieval"),
        )
        conn.commit()

    report = rebuild_backlinks(brain_root, auto_commit=False)

    links = rows(
        brain_root,
        """
        SELECT from_page, to_entity, relation, line_number, extracted_at
        FROM backlinks
        ORDER BY to_entity
        """,
    )
    assert report.scope == "backlinks"
    assert [tuple(row) for row in links] == [
        ("brain-project", "alice", "works_with", 14, utc_datetime(2).isoformat()),
    ]


def test_rebuild_pages_requires_force(tmp_path: Path) -> None:
    brain_root = tmp_path / "brain"
    write_brain_page(brain_root, "entities/alice.md", make_page(slug="alice", title="Alice"))

    with pytest.raises(BrainError, match="force=True"):
        rebuild_pages(brain_root, "alice", auto_commit=False)


def test_rebuild_pages_force_rewrites_truth_updates_timestamp_and_rebuilds_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain_root = tmp_path / "brain"
    brain_root.mkdir()
    init_db(brain_root / "brain.db")
    page_path = write_brain_page(
        brain_root,
        "entities/alice.md",
        make_page(slug="alice", title="Alice", tags=["person"], aliases=["Al"]),
    )
    with connect(brain_root / "brain.db") as conn:
        upsert_entity(conn, sample_entity("alice", "Alice", EntityType.PERSON))
        add_alias(conn, "Al", "alice", EntityAliasSource.FRONTMATTER)
        conn.commit()

    calls: list[tuple[int, str | None]] = []

    def fake_rewrite(timeline: list[Any], current_truth: str | None) -> str:
        calls.append((len(timeline), current_truth))
        return "new compiled truth"

    monkeypatch.setattr(rebuild_pipeline.llm_client, "rewrite_compiled_truth", fake_rewrite)

    before = parse_page(page_path)
    report = rebuild_pages(brain_root, "alice", force=True, auto_commit=False)
    after = parse_page(page_path)

    assert calls == [(1, "old truth")]
    assert report.scope == "pages"
    assert report.pages_touched == ["pages/entities/alice.md"]
    assert after.compiled_truth == "new compiled truth"
    assert after.frontmatter.updated != before.frontmatter.updated
    assert after.frontmatter.model_copy(update={"updated": before.frontmatter.updated}) == before.frontmatter
    assert after.timeline == before.timeline
    assert {row[4] for row in backlink_rows(brain_root)} == {
        after.frontmatter.updated.isoformat()
    }
    assert "- [Alice](entities/alice.md)" in (brain_root / "pages" / "index.md").read_text(
        encoding="utf-8"
    )


def test_rebuild_index_updates_pages_index(tmp_path: Path) -> None:
    brain_root = tmp_path / "brain"
    write_brain_page(brain_root, "entities/alice.md", make_page(slug="alice", title="Alice"))
    index_path = brain_root / "pages" / "index.md"
    index_path.write_text("stale\n", encoding="utf-8", newline="\n")

    report = rebuild_index(brain_root, auto_commit=False)

    assert report.scope == "index"
    assert report.index_rebuilt is True
    assert report.pages_touched == ["pages/index.md"]
    assert "- [Alice](entities/alice.md)" in index_path.read_text(encoding="utf-8")


def backlink_rows(brain_root: Path) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in rows(
            brain_root,
            """
            SELECT from_page, to_entity, relation, line_number, extracted_at
            FROM backlinks
            ORDER BY from_page, to_entity, relation
            """,
        )
    ]
