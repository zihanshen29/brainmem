import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.db.connection import connect
from brain.db.entities import add_alias, get_entity, upsert_entity
from brain.db.migrations import init_db
from brain.models import Entity, EntityAliasSource, EntityType, PageType, Tier
from brain.pipeline.autolink import extract_backlinks
from brain.pipeline.resolve import resolve_entity


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "brain.db"
    init_db(db_path)
    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def utc_datetime(day: int = 1) -> datetime:
    return datetime(2026, 4, day, 12, 0, tzinfo=UTC)


def sample_entity(
    entity_id: str = "zhang-san",
    title: str = "Zhang San",
    entity_type: EntityType = EntityType.PERSON,
    mention_count: int = 1,
) -> Entity:
    return Entity(
        id=entity_id,
        type=entity_type,
        title=title,
        page_path=f"pages/entities/{entity_id}.md",
        tier=Tier.TIER_3,
        mention_count=mention_count,
        first_seen=utc_datetime(),
        last_seen=utc_datetime(),
    )


def entity_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]


def test_resolve_alias_returns_canonical_and_updates_mention(conn: sqlite3.Connection) -> None:
    old_zhang = "\u8001\u5f20"
    upsert_entity(conn, sample_entity(mention_count=2))
    add_alias(conn, old_zhang, "zhang-san", EntityAliasSource.MANUAL)

    resolved = resolve_entity(conn, old_zhang, None)

    assert resolved is not None
    assert resolved.id == "zhang-san"
    assert resolved.mention_count == 3
    assert resolved.last_seen > utc_datetime()


def test_resolve_title_exact_match_updates_mention(conn: sqlite3.Connection) -> None:
    upsert_entity(conn, sample_entity("brain-project", "Brain Project", EntityType.PROJECT))

    resolved = resolve_entity(conn, "Brain Project", None)

    assert resolved is not None
    assert resolved.id == "brain-project"
    assert resolved.mention_count == 2
    assert resolved.last_seen > utc_datetime()


def test_resolve_ascii_name_creates_entity(conn: sqlite3.Connection) -> None:
    resolved = resolve_entity(conn, "Brain Dump", EntityType.PROJECT)

    assert resolved is not None
    assert resolved.id == "brain-dump"
    assert resolved.type is EntityType.PROJECT
    assert resolved.title == "Brain Dump"
    assert resolved.page_path == "pages/projects/brain-dump.md"
    assert resolved.tier is Tier.TIER_3
    assert resolved.mention_count == 1
    assert get_entity(conn, "brain-dump") == resolved


def test_resolve_ascii_name_without_hint_creates_person_entity_page(
    conn: sqlite3.Connection,
) -> None:
    resolved = resolve_entity(conn, "xiaozhang", None)

    assert resolved is not None
    assert resolved.id == "xiaozhang"
    assert resolved.type is EntityType.PERSON
    assert resolved.page_path == "pages/entities/xiaozhang.md"


@pytest.mark.parametrize(
    ("hint_type", "expected_page_path"),
    [
        (EntityType.CONCEPT, "pages/concepts/memory.md"),
        (EntityType.PROJECT, "pages/projects/memory.md"),
        (EntityType.EVENT, "pages/events/memory.md"),
    ],
)
def test_resolve_ascii_name_with_page_type_hint_uses_specialized_page_path(
    conn: sqlite3.Connection,
    hint_type: EntityType,
    expected_page_path: str,
) -> None:
    resolved = resolve_entity(conn, "Memory", hint_type)

    assert resolved is not None
    assert resolved.type is hint_type
    assert resolved.page_path == expected_page_path


def test_resolve_existing_slug_updates_mention(conn: sqlite3.Connection) -> None:
    upsert_entity(conn, sample_entity("brain-dump", "Brain Dump", EntityType.PROJECT))

    resolved = resolve_entity(conn, "brain-dump", EntityType.PROJECT)

    assert resolved is not None
    assert resolved.id == "brain-dump"
    assert resolved.mention_count == 2


def test_resolve_ascii_slug_compact_match_reuses_existing_entity(
    conn: sqlite3.Connection,
) -> None:
    upsert_entity(conn, sample_entity("xiao-zhang", "Xiao Zhang", EntityType.PERSON))

    resolved = resolve_entity(conn, "xiaozhang", None)

    assert resolved is not None
    assert resolved.id == "xiao-zhang"
    assert resolved.mention_count == 2
    assert entity_count(conn) == 1


def test_resolve_non_ascii_first_entity_returns_none_without_db_write(
    conn: sqlite3.Connection,
) -> None:
    before = entity_count(conn)

    resolved = resolve_entity(conn, "\u65b0\u670b\u53cb", EntityType.PERSON)

    assert resolved is None
    assert entity_count(conn) == before


def test_extract_backlinks_alias_wikilink_dedupe_and_line_numbers() -> None:
    old_zhang = "\u8001\u5f20"
    content = "\n".join(
        [
            f"{old_zhang} helped with Retrieval.",
            f"Repeated {old_zhang} mention should dedupe.",
            "See [[brain-dump|Brain Dump]] and [[zhang-san]].",
        ]
    )

    links = extract_backlinks(
        content,
        alias_map={old_zhang: "zhang-san", "Retrieval": "retrieval"},
        from_page="brain-project",
        from_page_type=PageType.PROJECT,
        entity_types={
            "zhang-san": EntityType.PERSON,
            "retrieval": EntityType.CONCEPT,
            "brain-dump": EntityType.PROJECT,
        },
    )

    assert [(link.to_entity, link.relation, link.line_number) for link in links] == [
        ("zhang-san", "works_with", 1),
        ("retrieval", "involves", 1),
        ("brain-dump", "mentions", 3),
        ("zhang-san", "mentions", 3),
    ]


def test_extract_backlinks_does_not_scan_alias_inside_wikilink_display() -> None:
    links = extract_backlinks(
        "See [[zhang-san|\u8001\u5f20]].",
        alias_map={"\u8001\u5f20": "zhang-san"},
        from_page="project",
        from_page_type=PageType.PROJECT,
        entity_types={"zhang-san": EntityType.PERSON},
    )

    assert [(link.to_entity, link.relation, link.line_number) for link in links] == [
        ("zhang-san", "mentions", 1),
    ]


def test_extract_backlinks_ascii_aliases_are_case_insensitive_whole_words() -> None:
    content = "\n".join(
        [
            "AI helps, ai helps again, but Tail Said AIM and ai@example.com do not.",
            "Go went, go returned, but going ego logo and user@go.dev do not.",
            "\u8001\u5f20\u53c2\u4e0e\uff0c\u800c\u4e14\u5c0f\u8001\u5f20\u4e5f\u4fdd\u6301\u5b50\u4e32\u5339\u914d.",
        ]
    )

    links = extract_backlinks(
        content,
        alias_map={
            "AI": "artificial-intelligence",
            "Go": "go",
            "\u8001\u5f20": "zhang-san",
        },
        from_page="source",
        entity_types={
            "artificial-intelligence": EntityType.CONCEPT,
            "go": EntityType.CONCEPT,
            "zhang-san": EntityType.PERSON,
        },
    )

    assert [(link.to_entity, link.line_number) for link in links] == [
        ("artificial-intelligence", 1),
        ("go", 2),
        ("zhang-san", 3),
    ]


@pytest.mark.parametrize(
    ("page_type", "entity_type", "expected_relation"),
    [
        (PageType.PROJECT, EntityType.PERSON, "works_with"),
        (PageType.PROJECT, EntityType.CONCEPT, "involves"),
        (PageType.EVENT, EntityType.PERSON, "attended_by"),
        (PageType.CONVERSATION, EntityType.PERSON, "participant"),
        (PageType.CONCEPT, EntityType.PERSON, "mentions"),
    ],
)
def test_extract_backlinks_infers_alias_relations(
    page_type: PageType,
    entity_type: EntityType,
    expected_relation: str,
) -> None:
    links = extract_backlinks(
        "Alias",
        alias_map={"Alias": "target"},
        from_page="source",
        from_page_type=page_type,
        entity_types={"target": entity_type},
    )

    assert len(links) == 1
    assert links[0].relation == expected_relation
