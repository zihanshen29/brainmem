import sqlite3

from brain.models import Backlink


def _backlink_from_row(row: sqlite3.Row) -> Backlink:
    """Build a Backlink model from a SQLite row."""
    return Backlink(
        from_page=row["from_page"],
        to_entity=row["to_entity"],
        relation=row["relation"],
        line_number=row["line_number"],
        extracted_at=row["extracted_at"],
    )


def replace_backlinks_for_page(
    conn: sqlite3.Connection,
    page_slug: str,
    links: list[Backlink],
) -> None:
    """Replace all backlinks extracted from one page."""
    conn.execute("DELETE FROM backlinks WHERE from_page = ?", (page_slug,))
    conn.executemany(
        """
        INSERT INTO backlinks (from_page, to_entity, relation, line_number, extracted_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                page_slug,
                link.to_entity,
                link.relation,
                link.line_number,
                link.extracted_at.isoformat(),
            )
            for link in links
        ],
    )


def get_backlinks_to(conn: sqlite3.Connection, entity_id: str) -> list[Backlink]:
    """Return backlinks pointing to an entity."""
    rows = conn.execute(
        "SELECT * FROM backlinks WHERE to_entity = ? ORDER BY from_page, relation",
        (entity_id,),
    ).fetchall()
    return [_backlink_from_row(row) for row in rows]
