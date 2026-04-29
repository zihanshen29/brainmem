import re
import sqlite3
from datetime import UTC, datetime

from brain.db.entities import get_entity, lookup_by_alias, upsert_entity
from brain.models import Entity, EntityType, Tier

_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9]+")
_DASH_PATTERN = re.compile(r"-+")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _touch_entity(conn: sqlite3.Connection, entity_id: str) -> Entity | None:
    last_seen = _now_utc()
    conn.execute(
        """
        UPDATE entities
        SET mention_count = mention_count + 1,
            last_seen = ?
        WHERE id = ?
        """,
        (last_seen.isoformat(), entity_id),
    )
    return get_entity(conn, entity_id)


def _slug_from_name(name: str) -> str | None:
    if not name.isascii():
        return None

    slug = _NON_ALNUM_PATTERN.sub("-", name.lower())
    slug = _DASH_PATTERN.sub("-", slug).strip("-")
    return slug or None


def _page_path_for_entity(entity_id: str, entity_type: EntityType | None) -> str:
    if entity_type is EntityType.PROJECT:
        return f"pages/projects/{entity_id}.md"
    if entity_type is EntityType.CONCEPT:
        return f"pages/concepts/{entity_id}.md"
    if entity_type is EntityType.EVENT:
        return f"pages/events/{entity_id}.md"
    return f"pages/entities/{entity_id}.md"


def resolve_entity(
    conn: sqlite3.Connection,
    name: str,
    hint_type: EntityType | None,
) -> Entity | None:
    """Resolve an entity by exact alias/title, or create a new ASCII-slug entity."""
    alias_entity_id = lookup_by_alias(conn, name)
    if alias_entity_id is not None:
        return _touch_entity(conn, alias_entity_id)

    title_row = conn.execute(
        "SELECT id FROM entities WHERE title = ? ORDER BY id LIMIT 1",
        (name,),
    ).fetchone()
    if title_row is not None:
        return _touch_entity(conn, title_row["id"])

    slug = _slug_from_name(name)
    if slug is None:
        return None

    existing = get_entity(conn, slug)
    if existing is not None:
        return _touch_entity(conn, existing.id)

    compact_match = _lookup_by_compact_slug(conn, slug)
    if compact_match is not None:
        return _touch_entity(conn, compact_match)

    now = _now_utc()
    entity = Entity(
        id=slug,
        type=hint_type or EntityType.CONCEPT,
        title=name,
        page_path=_page_path_for_entity(slug, hint_type),
        tier=Tier.TIER_3,
        mention_count=1,
        first_seen=now,
        last_seen=now,
    )
    upsert_entity(conn, entity)
    return entity


def _lookup_by_compact_slug(conn: sqlite3.Connection, slug: str) -> str | None:
    compact_slug = slug.replace("-", "")
    rows = conn.execute(
        """
        SELECT id
        FROM entities
        WHERE replace(id, '-', '') = ?
        ORDER BY id
        """,
        (compact_slug,),
    ).fetchall()
    if len(rows) != 1:
        return None
    return str(rows[0]["id"])
