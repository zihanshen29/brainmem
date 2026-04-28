from __future__ import annotations

import sqlite3
from enum import StrEnum

from brain.config import Config
from brain.db.facts import find_active_facts
from brain.llm import client as llm_client
from brain.models import Fact, FactCandidate
from brain.pipeline._config import resolve_pipeline_config


class Decision(StrEnum):
    """Classification for a fact candidate against active facts."""

    ADD = "ADD"
    NOOP = "NOOP"
    SUPERSEDE = "SUPERSEDE"
    CONFLICT = "CONFLICT"


def classify_fact(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
    config: Config | None = None,
) -> Decision:
    """Classify a candidate fact without mutating persisted facts."""
    active_facts = find_active_facts(conn, candidate.subject, candidate.predicate)
    if not active_facts:
        return Decision.ADD

    if any(fact.object == candidate.object for fact in active_facts):
        return Decision.NOOP

    resolved_config = resolve_pipeline_config(config)
    auto_accept = resolved_config.ingest.confidence_auto_accept
    if candidate.confidence < auto_accept:
        return Decision.CONFLICT

    if any(fact.confidence >= auto_accept for fact in active_facts):
        return Decision.CONFLICT

    return _judge_low_confidence_conflicts(active_facts, candidate, resolved_config)


def _judge_low_confidence_conflicts(
    old_facts: list[Fact],
    candidate: FactCandidate,
    config: Config,
) -> Decision:
    for old_fact in old_facts:
        judgment = llm_client.judge_conflict(old_fact, candidate)
        if not judgment.is_conflict or not judgment.new_supersedes_old:
            return Decision.CONFLICT
        if judgment.confidence < config.ingest.confidence_auto_accept:
            return Decision.CONFLICT
    return Decision.SUPERSEDE
