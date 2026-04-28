import sqlite3
from datetime import UTC, datetime

from brain.models import Tier


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def propose_tier(
    conn: sqlite3.Connection,
    entity_id: str,
    target_tier: Tier | int,
    reason: str,
    review_file: str,
) -> int:
    """Insert a tier proposal for review.

    Returns:
        The inserted proposal id.
    """
    row = conn.execute("SELECT tier FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise sqlite3.IntegrityError(f"entity not found: {entity_id}")

    cursor = conn.execute(
        """
        INSERT INTO tier_proposals (
            entity_id, proposed_tier, current_tier, reason, proposed_at, review_file
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entity_id, int(target_tier), row["tier"], reason, _now_iso(), review_file),
    )
    return int(cursor.lastrowid)


def record_tier_decision(conn: sqlite3.Connection, proposal_id: int, decision: str) -> None:
    """Record a user decision for a tier proposal."""
    conn.execute(
        """
        UPDATE tier_proposals
        SET decided_at = ?, decision = ?
        WHERE id = ?
        """,
        (_now_iso(), decision, proposal_id),
    )
