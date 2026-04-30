import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.db.backlinks import get_backlinks_to, replace_backlinks_for_page
from brain.db.connection import connect
from brain.db.entities import (
    add_alias,
    get_entity,
    increment_mention,
    lookup_by_alias,
    upsert_entity,
)
from brain.db.facts import add_fact, find_active_facts, supersede
from brain.db.migrations import init_db
from brain.db.tier import propose_tier, record_tier_decision
from brain.models import (
    Backlink,
    Entity,
    EntityAliasSource,
    EntityType,
    Fact,
    FactObjectType,
    Tier,
)

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "brain.db"
    init_db(path)
    return path


@pytest.fixture()
def conn(db_path: Path):
    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def utc_datetime(day: int = 28) -> datetime:
    return datetime(2026, 4, day, 12, 0, tzinfo=UTC)


def sample_entity(entity_id: str = "zhang-san", title: str = "Zhang San") -> Entity:
    return Entity(
        id=entity_id,
        type=EntityType.PERSON,
        title=title,
        page_path=f"pages/entities/{entity_id}.md",
        tier=Tier.TIER_3,
        mention_count=1,
        first_seen=utc_datetime(),
        last_seen=utc_datetime(),
        metadata={"origin": "test"},
    )


def sample_fact(object_value: str = "UK", event_id: str = VALID_ULID) -> Fact:
    return Fact(
        subject="zihan",
        predicate="location",
        object=object_value,
        object_type=FactObjectType.LITERAL,
        valid_from="2026-04-01",
        asserted_at=utc_datetime(),
        source_event=event_id,
        source_ref="events.jsonl",
        confidence=0.9,
    )


def test_init_db_creates_schema_and_sets_version(db_path: Path) -> None:
    with connect(db_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()

    tables = {row["name"] for row in table_rows}

    assert version == 2
    assert {
        "entities",
        "entity_aliases",
        "facts",
        "backlinks",
        "tier_proposals",
        "ingest_cursor",
        "lint_results",
        "embedding_index",
        "import_jobs",
        "import_files",
        "stats",
    }.issubset(tables)


def test_connect_enables_foreign_keys_and_wal(db_path: Path) -> None:
    connection = connect(db_path)
    try:
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    assert foreign_keys == 1
    assert journal_mode == "wal"


def test_entity_upsert_and_lookup_round_trip(conn: sqlite3.Connection) -> None:
    entity = sample_entity()

    entity_id = upsert_entity(conn, entity)
    loaded = get_entity(conn, entity_id)

    assert entity_id == "zhang-san"
    assert loaded == entity

    updated = entity.model_copy(update={"mention_count": 3, "metadata": {"updated": True}})
    upsert_entity(conn, updated)

    assert get_entity(conn, entity_id) == updated


def test_alias_unique_constraint_and_lookup(conn: sqlite3.Connection) -> None:
    upsert_entity(conn, sample_entity("zhang-san", "Zhang San"))
    upsert_entity(conn, sample_entity("li-si", "Li Si"))

    add_alias(conn, "老张", "zhang-san", EntityAliasSource.MANUAL)

    assert lookup_by_alias(conn, "老张") == "zhang-san"
    with pytest.raises(sqlite3.IntegrityError):
        add_alias(conn, "老张", "li-si", EntityAliasSource.AUTO_DETECTED)


def test_increment_mention(conn: sqlite3.Connection) -> None:
    upsert_entity(conn, sample_entity())

    increment_mention(conn, "zhang-san")

    loaded = get_entity(conn, "zhang-san")
    assert loaded is not None
    assert loaded.mention_count == 2


def test_add_find_and_supersede_facts(conn: sqlite3.Connection) -> None:
    old_id = add_fact(conn, sample_fact("UK", VALID_ULID))
    new_id = add_fact(conn, sample_fact("Singapore", SECOND_ULID))

    active_before = find_active_facts(conn, "zihan", "location")
    assert [fact.id for fact in active_before] == [old_id, new_id]

    supersede(conn, old_id, new_id)

    active_after = find_active_facts(conn, "zihan", "location")
    old_superseded_by = conn.execute(
        "SELECT superseded_by FROM facts WHERE id = ?",
        (old_id,),
    ).fetchone()["superseded_by"]

    assert [fact.id for fact in active_after] == [new_id]
    assert old_superseded_by == new_id


def test_find_active_facts_excludes_valid_to(conn: sqlite3.Connection) -> None:
    inactive = sample_fact().model_copy(update={"valid_to": "2026-05-01"})
    add_fact(conn, inactive)

    assert find_active_facts(conn, "zihan", "location") == []


def test_replace_and_get_backlinks(conn: sqlite3.Connection) -> None:
    upsert_entity(conn, sample_entity("zhang-san", "Zhang San"))
    upsert_entity(conn, sample_entity("zihan", "Zihan"))
    first_link = Backlink(
        from_page="ignored",
        to_entity="zhang-san",
        relation="works_with",
        line_number=12,
        extracted_at=utc_datetime(),
    )
    second_link = Backlink(
        from_page="cv-coursework",
        to_entity="zihan",
        relation="mentions",
        line_number=4,
        extracted_at=utc_datetime(),
    )

    replace_backlinks_for_page(conn, "cv-coursework", [first_link])
    assert get_backlinks_to(conn, "zhang-san") == [
        first_link.model_copy(update={"from_page": "cv-coursework"})
    ]

    replace_backlinks_for_page(conn, "cv-coursework", [second_link])

    assert get_backlinks_to(conn, "zhang-san") == []
    assert get_backlinks_to(conn, "zihan") == [second_link]


def test_tier_proposal_and_decision(conn: sqlite3.Connection) -> None:
    upsert_entity(conn, sample_entity())

    proposal_id = propose_tier(
        conn,
        entity_id="zhang-san",
        target_tier=Tier.TIER_2,
        reason="mention_count_3",
        review_file="review/2026-04-28_001_tier_proposal.md",
    )
    row = conn.execute("SELECT * FROM tier_proposals WHERE id = ?", (proposal_id,)).fetchone()

    assert row["entity_id"] == "zhang-san"
    assert row["proposed_tier"] == 2
    assert row["current_tier"] == 3
    assert row["decision"] is None

    record_tier_decision(conn, proposal_id, "approved")
    decided = conn.execute("SELECT * FROM tier_proposals WHERE id = ?", (proposal_id,)).fetchone()

    assert decided["decision"] == "approved"
    assert decided["decided_at"] is not None


def test_transaction_rollback_discards_uncommitted_data(db_path: Path) -> None:
    connection = connect(db_path)
    try:
        try:
            connection.execute("BEGIN")
            upsert_entity(connection, sample_entity())
            raise RuntimeError("force rollback")
        except RuntimeError:
            connection.rollback()

        assert get_entity(connection, "zhang-san") is None
    finally:
        connection.close()
