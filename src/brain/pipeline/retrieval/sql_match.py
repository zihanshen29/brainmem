from __future__ import annotations

import sqlite3

from brain.models import RetrievalHit
from brain.pipeline.retrieval.keyword import tokenize


def sql_entity_match(conn: sqlite3.Connection, query: str, top: int = 50) -> list[RetrievalHit]:
    """Match entity titles and aliases, then add entity pages and backlink sources."""
    if top <= 0:
        return []

    entity_ids = _matched_entity_ids(conn, query)
    if not entity_ids:
        return []

    hits: list[RetrievalHit] = []
    seen_pages: set[str] = set()
    for entity_id in sorted(entity_ids):
        page = _entity_page_slug(conn, entity_id)
        if page is None or page in seen_pages:
            continue
        seen_pages.add(page)
        hits.append(_hit(page, 10.0, len(hits) + 1))

    for row in _backlink_rows(conn, sorted(entity_ids), max(top - len(hits), 0)):
        page = str(row["from_page"])
        if page in seen_pages:
            continue
        seen_pages.add(page)
        hits.append(_hit(page, float(row["link_count"]), len(hits) + 1))
        if len(hits) >= top:
            break

    return hits[:top]


def _matched_entity_ids(conn: sqlite3.Connection, query: str) -> set[str]:
    terms = _terms(query)
    matched: set[str] = set()
    for term in terms:
        for row in conn.execute(
            "SELECT id FROM entities WHERE title = ? COLLATE NOCASE",
            (term,),
        ).fetchall():
            matched.add(str(row["id"]))
        for row in conn.execute(
            "SELECT entity_id FROM entity_aliases WHERE alias = ? COLLATE NOCASE",
            (term,),
        ).fetchall():
            matched.add(str(row["entity_id"]))
    return matched


def _terms(query: str) -> list[str]:
    stripped = query.strip()
    return [term for term in dict.fromkeys([stripped, stripped.lower(), *tokenize(stripped)]) if term]


def _entity_page_slug(conn: sqlite3.Connection, entity_id: str) -> str | None:
    row = conn.execute(
        "SELECT id, page_path FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    page_path = row["page_path"]
    if isinstance(page_path, str) and page_path:
        return page_path.rsplit("/", 1)[-1].removesuffix(".md")
    return str(row["id"])


def _backlink_rows(
    conn: sqlite3.Connection,
    entity_ids: list[str],
    limit: int,
) -> list[sqlite3.Row]:
    if not entity_ids or limit <= 0:
        return []
    placeholders = ",".join("?" for _ in entity_ids)
    return conn.execute(
        f"""
        SELECT from_page, COUNT(*) AS link_count
        FROM backlinks
        WHERE to_entity IN ({placeholders})
        GROUP BY from_page
        ORDER BY link_count DESC, from_page ASC
        LIMIT ?
        """,
        (*entity_ids, limit),
    ).fetchall()


def _hit(page_slug: str, score: float, rank: int) -> RetrievalHit:
    return RetrievalHit(
        page_slug=page_slug,
        chunk_kind="compiled_truth",
        chunk_id="main",
        score=score,
        rank=rank,
        path="sql",
    )
