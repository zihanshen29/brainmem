import json
import sqlite3

from brain.models import Entity, EntityAliasSource


def _entity_from_row(row: sqlite3.Row) -> Entity:
    """Build an Entity model from a SQLite row."""
    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    return Entity(
        id=row["id"],
        type=row["type"],
        title=row["title"],
        page_path=row["page_path"],
        tier=row["tier"],
        mention_count=row["mention_count"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        metadata=metadata,
    )


def upsert_entity(conn: sqlite3.Connection, entity: Entity) -> str:
    """Insert or replace an entity registry row.

    Args:
        conn: Open SQLite connection.
        entity: Entity to persist.

    Returns:
        The canonical entity id.
    """
    conn.execute(
        """
        INSERT INTO entities (
            id, type, title, page_path, tier, mention_count, first_seen, last_seen, metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            type = excluded.type,
            title = excluded.title,
            page_path = excluded.page_path,
            tier = excluded.tier,
            mention_count = excluded.mention_count,
            first_seen = excluded.first_seen,
            last_seen = excluded.last_seen,
            metadata = excluded.metadata
        """,
        (
            entity.id,
            entity.type.value,
            entity.title,
            entity.page_path,
            int(entity.tier),
            entity.mention_count,
            entity.first_seen.isoformat(),
            entity.last_seen.isoformat(),
            json.dumps(entity.metadata, ensure_ascii=False),
        ),
    )
    return entity.id


def get_entity(conn: sqlite3.Connection, id: str) -> Entity | None:
    """Return an entity by id, or None when missing."""
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (id,)).fetchone()
    if row is None:
        return None
    return _entity_from_row(row)


def add_alias(
    conn: sqlite3.Connection,
    alias: str,
    entity_id: str,
    source: EntityAliasSource | str,
) -> None:
    """Add an alias for a canonical entity."""
    source_value = source.value if isinstance(source, EntityAliasSource) else source
    conn.execute(
        "INSERT INTO entity_aliases (alias, entity_id, source) VALUES (?, ?, ?)",
        (alias, entity_id, source_value),
    )


def lookup_by_alias(conn: sqlite3.Connection, alias: str) -> str | None:
    """Look up the canonical entity id for an alias."""
    row = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE alias = ?",
        (alias,),
    ).fetchone()
    if row is None:
        return None
    return row["entity_id"]


def increment_mention(conn: sqlite3.Connection, id: str) -> None:
    """Increment an entity's mention count."""
    conn.execute(
        "UPDATE entities SET mention_count = mention_count + 1 WHERE id = ?",
        (id,),
    )
