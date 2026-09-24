import hashlib
import re
import sqlite3
import unicodedata
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
        return "entity-" + hashlib.sha256(normalize_name(name).encode()).hexdigest()[:12]

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


def _entity_type_for_hint(hint_type: EntityType | None) -> EntityType:
    return hint_type or EntityType.UNKNOWN


def normalize_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def matching_entity_ids(conn: sqlite3.Connection, name: str) -> list[str]:
    key = normalize_name(name)
    rows = conn.execute(
        "SELECT id AS entity_id, id AS name FROM entities UNION ALL "
        "SELECT id, title FROM entities UNION ALL SELECT entity_id, alias FROM entity_aliases"
    )
    return sorted({row["entity_id"] for row in rows if normalize_name(row["name"]) == key})


def resolve_entity(
    conn: sqlite3.Connection,
    name: str,
    hint_type: EntityType | None,
    *,
    confidence: float = 1.0,
    allow_create: bool = True,
    auto_accept: float = 0.85,
) -> Entity | None:
    """Use unique matches first; new typed entities have the same gate in every language."""
    matches = matching_entity_ids(conn, name)
    if len(matches) == 1:
        return _touch_entity(conn, matches[0])
    if len(matches) > 1:
        return None
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

    if not allow_create or hint_type in {None, EntityType.UNKNOWN} or confidence < auto_accept:
        return None

    now = _now_utc()
    entity_type = _entity_type_for_hint(hint_type)
    entity = Entity(
        id=slug,
        type=entity_type,
        title=name,
        page_path=_page_path_for_entity(slug, entity_type),
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
