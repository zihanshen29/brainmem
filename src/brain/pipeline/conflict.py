from __future__ import annotations

import sqlite3
from enum import StrEnum

from brain.config import Config
from brain.db.facts import find_active_facts
from brain.models import FactCandidate
from brain.predicates import is_single_valued


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

    if not is_single_valued(candidate.predicate):
        return Decision.ADD

    # Different values for known single-valued predicates require human review.
    # This path never calls a model while applying database changes.
    return Decision.CONFLICT
