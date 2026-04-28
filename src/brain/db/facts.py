import sqlite3

from brain.models import Fact


def _fact_from_row(row: sqlite3.Row) -> Fact:
    """Build a Fact model from a SQLite row."""
    return Fact(
        id=row["id"],
        subject=row["subject"],
        predicate=row["predicate"],
        object=row["object"],
        object_type=row["object_type"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        asserted_at=row["asserted_at"],
        source_event=row["source_event"],
        source_ref=row["source_ref"],
        confidence=row["confidence"],
        superseded_by=row["superseded_by"],
    )


def add_fact(conn: sqlite3.Connection, fact: Fact) -> int:
    """Insert a structured fact.

    Args:
        conn: Open SQLite connection.
        fact: Fact to persist.

    Returns:
        The inserted fact id.
    """
    if fact.id is None:
        cursor = conn.execute(
            """
            INSERT INTO facts (
                subject, predicate, object, object_type, valid_from, valid_to,
                asserted_at, source_event, source_ref, confidence, superseded_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.subject,
                fact.predicate,
                fact.object,
                fact.object_type.value,
                fact.valid_from,
                fact.valid_to,
                fact.asserted_at.isoformat(),
                fact.source_event,
                fact.source_ref,
                fact.confidence,
                fact.superseded_by,
            ),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO facts (
                id, subject, predicate, object, object_type, valid_from, valid_to,
                asserted_at, source_event, source_ref, confidence, superseded_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.id,
                fact.subject,
                fact.predicate,
                fact.object,
                fact.object_type.value,
                fact.valid_from,
                fact.valid_to,
                fact.asserted_at.isoformat(),
                fact.source_event,
                fact.source_ref,
                fact.confidence,
                fact.superseded_by,
            ),
        )
    return int(cursor.lastrowid if fact.id is None else fact.id)


def find_active_facts(conn: sqlite3.Connection, subject: str, predicate: str) -> list[Fact]:
    """Find active facts for a subject and predicate."""
    rows = conn.execute(
        """
        SELECT * FROM facts
        WHERE subject = ?
          AND predicate = ?
          AND superseded_by IS NULL
          AND valid_to IS NULL
        ORDER BY id
        """,
        (subject, predicate),
    ).fetchall()
    return [_fact_from_row(row) for row in rows]


def supersede(conn: sqlite3.Connection, old_fact_id: int, new_fact_id: int) -> None:
    """Mark one fact as superseded by another."""
    conn.execute(
        "UPDATE facts SET superseded_by = ? WHERE id = ?",
        (new_fact_id, old_fact_id),
    )
