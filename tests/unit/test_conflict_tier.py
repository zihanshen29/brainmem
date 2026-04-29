import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.config import (
    Config,
    DeepSeekConfig,
    GitConfig,
    IngestConfig,
    LintConfig,
    PathsConfig,
    TierConfig,
)
from brain.db.connection import connect
from brain.db.entities import upsert_entity
from brain.db.facts import add_fact
from brain.db.migrations import init_db
from brain.llm import client as llm_client
from brain.llm.client import ConflictJudgment
from brain.models import Entity, EntityType, Fact, FactCandidate, FactObjectType, Tier
from brain.pipeline import Decision, TierProposal, check_tier_upgrade, classify_fact

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


def make_config(auto_accept: float = 0.85, auto_reject: float = 0.50) -> Config:
    return Config(
        deepseek=DeepSeekConfig(
            api_key_env="DEEPSEEK_API_KEY",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            fast_model="deepseek-v4-flash",
        ),
        paths=PathsConfig(brain_root=Path.cwd()),
        ingest=IngestConfig(
            confidence_auto_accept=auto_accept,
            confidence_auto_reject=auto_reject,
        ),
        tier=TierConfig(tier3_threshold=1, tier2_threshold=3, tier1_threshold=8),
        lint=LintConfig(stale_days=90),
        git=GitConfig(auto_commit=False),
    )


def sample_fact(object_value: str = "UK", confidence: float = 0.9) -> Fact:
    return Fact(
        subject="zihan",
        predicate="location",
        object=object_value,
        object_type=FactObjectType.LITERAL,
        valid_from="2026-04-01",
        asserted_at=utc_datetime(),
        source_event=VALID_ULID,
        source_ref="events.jsonl",
        confidence=confidence,
    )


def sample_candidate(object_value: str = "Singapore", confidence: float = 0.9) -> FactCandidate:
    return FactCandidate(
        subject="zihan",
        predicate="location",
        object=object_value,
        object_type=FactObjectType.LITERAL,
        valid_from="2026-04-28",
        source_event=SECOND_ULID,
        source_ref="laundry/zihan.md",
        confidence=confidence,
    )


def sample_entity(
    entity_id: str = "zihan",
    tier: Tier = Tier.TIER_3,
    mention_count: int = 1,
) -> Entity:
    return Entity(
        id=entity_id,
        type=EntityType.PERSON,
        title="Zihan",
        page_path=f"pages/entities/{entity_id}.md",
        tier=tier,
        mention_count=mention_count,
        first_seen=utc_datetime(),
        last_seen=utc_datetime(),
        metadata={},
    )


def row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])


def test_classify_fact_no_active_facts_add(conn: sqlite3.Connection) -> None:
    assert classify_fact(conn, sample_candidate()) is Decision.ADD


def test_classify_fact_identical_object_noop(conn: sqlite3.Connection) -> None:
    add_fact(conn, sample_fact("UK"))

    decision = classify_fact(conn, sample_candidate("UK"), config=make_config())

    assert decision is Decision.NOOP


def test_classify_fact_different_object_conflict(conn: sqlite3.Connection) -> None:
    add_fact(conn, sample_fact("UK", confidence=0.9))

    decision = classify_fact(conn, sample_candidate("Singapore", confidence=0.9), make_config())

    assert decision is Decision.CONFLICT


def test_classify_fact_llm_supersede_decision(conn: sqlite3.Connection, monkeypatch) -> None:
    add_fact(conn, sample_fact("UK", confidence=0.4))
    calls: list[tuple[Fact, FactCandidate]] = []

    def fake_judge(old: Fact, new: FactCandidate) -> ConflictJudgment:
        calls.append((old, new))
        return ConflictJudgment(
            is_conflict=True,
            new_supersedes_old=True,
            reason="newer high confidence fact",
            confidence=0.92,
        )

    monkeypatch.setattr(llm_client, "judge_conflict", fake_judge)

    decision = classify_fact(conn, sample_candidate("Singapore", confidence=0.95), make_config())

    assert decision is Decision.SUPERSEDE
    assert len(calls) == 1


def test_classify_fact_adds_when_llm_says_no_conflict(
    conn: sqlite3.Connection,
    monkeypatch,
) -> None:
    add_fact(conn, sample_fact("UK", confidence=0.4))

    monkeypatch.setattr(
        llm_client,
        "judge_conflict",
        lambda old, new: ConflictJudgment(
            is_conflict=False,
            new_supersedes_old=True,
            reason="not an actual conflict",
            confidence=0.92,
        ),
    )

    decision = classify_fact(conn, sample_candidate("Singapore", confidence=0.95), make_config())

    assert decision is Decision.ADD


def test_classify_fact_does_not_write_db(conn: sqlite3.Connection, monkeypatch) -> None:
    fact_id = add_fact(conn, sample_fact("UK", confidence=0.4))

    monkeypatch.setattr(
        llm_client,
        "judge_conflict",
        lambda old, new: ConflictJudgment(
            is_conflict=True,
            new_supersedes_old=True,
            reason="newer high confidence fact",
            confidence=0.92,
        ),
    )

    decision = classify_fact(conn, sample_candidate("Singapore", confidence=0.95), make_config())
    row = conn.execute("SELECT superseded_by FROM facts WHERE id = ?", (fact_id,)).fetchone()

    assert decision is Decision.SUPERSEDE
    assert row_count(conn, "facts") == 1
    assert row["superseded_by"] is None


@pytest.mark.parametrize(
    ("tier", "mention_count", "expected_tier"),
    [
        (Tier.TIER_3, 3, Tier.TIER_2),
        (Tier.TIER_2, 8, Tier.TIER_1),
    ],
)
def test_check_tier_upgrade_crosses_threshold_returns_proposal(
    conn: sqlite3.Connection,
    tier: Tier,
    mention_count: int,
    expected_tier: Tier,
) -> None:
    upsert_entity(conn, sample_entity(tier=tier, mention_count=mention_count))

    proposal = check_tier_upgrade(conn, "zihan", make_config())

    assert proposal == TierProposal(
        entity_id="zihan",
        current_tier=tier,
        proposed_tier=expected_tier,
        reason=f"mention_count {mention_count} reached tier {int(expected_tier)} threshold",
        mention_count=mention_count,
    )
    assert row_count(conn, "tier_proposals") == 0


@pytest.mark.parametrize(
    ("tier", "mention_count"),
    [
        (Tier.TIER_3, 2),
        (Tier.TIER_2, 7),
        (Tier.TIER_1, 100),
    ],
)
def test_check_tier_upgrade_no_threshold_crossing_or_tier_one_returns_none(
    conn: sqlite3.Connection,
    tier: Tier,
    mention_count: int,
) -> None:
    upsert_entity(conn, sample_entity(tier=tier, mention_count=mention_count))

    assert check_tier_upgrade(conn, "zihan", make_config()) is None


def test_check_tier_upgrade_missing_entity_returns_none(conn: sqlite3.Connection) -> None:
    assert check_tier_upgrade(conn, "missing", make_config()) is None
