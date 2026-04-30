from __future__ import annotations

import re
import sqlite3

from brain.models import FusedResult, RetrievalHit
from brain.pipeline.retrieval.classifier import classify_query
from brain.pipeline.retrieval.sql_match import _matched_entity_ids

DATE_RE = re.compile(r"(\d{4}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?|\d{4}\s*\u5e74)")


def sql_direct_query(conn: sqlite3.Connection, query: str, top: int = 50) -> list[FusedResult]:
    """Conservative structured-query shortcut using fixed SQL templates only."""
    if top <= 0 or classify_query(query) != "structured":
        return []

    entity_ids = sorted(_matched_entity_ids_for_query(conn, query))
    date = _date_fragment(query)
    if not entity_ids and date is None:
        return []

    clauses = ["superseded_by IS NULL"]
    params: list[str | int] = []
    if entity_ids:
        placeholders = ",".join("?" for _ in entity_ids)
        clauses.append(f"(subject IN ({placeholders}) OR object IN ({placeholders}))")
        params.extend(entity_ids)
        params.extend(entity_ids)
    if date is not None:
        clauses.append(
            "(valid_from LIKE ? OR valid_to LIKE ? OR asserted_at LIKE ? OR object LIKE ?)"
        )
        params.extend([f"{date}%", f"{date}%", f"{date}%", f"%{date}%"])

    params.append(top * 10)
    rows = conn.execute(
        f"""
        SELECT subject, predicate, object, object_type, source_event, source_ref
        FROM facts
        WHERE {" AND ".join(clauses)}
        ORDER BY asserted_at DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return _fused_from_fact_rows(conn, rows)[:top]


def _date_fragment(query: str) -> str | None:
    match = DATE_RE.search(query)
    if match is None:
        return None
    return match.group(1).replace("/", "-").replace("\u5e74", "").strip()


def _matched_entity_ids_for_query(conn: sqlite3.Connection, query: str) -> set[str]:
    matched = _matched_entity_ids(conn, query)
    for row in conn.execute(
        """
        SELECT id
        FROM entities
        WHERE title <> '' AND ? LIKE '%' || title || '%' COLLATE NOCASE
        """,
        (query,),
    ).fetchall():
        matched.add(str(row["id"]))
    for row in conn.execute(
        """
        SELECT entity_id
        FROM entity_aliases
        WHERE alias <> '' AND ? LIKE '%' || alias || '%' COLLATE NOCASE
        """,
        (query,),
    ).fetchall():
        matched.add(str(row["entity_id"]))
    return matched


def _fused_from_fact_rows(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> list[FusedResult]:
    by_page: dict[str, FusedResult] = {}
    for rank, row in enumerate(rows, start=1):
        page_slug = _canonical_page_slug(conn, row)
        hit = RetrievalHit(
            page_slug=page_slug,
            chunk_kind="compiled_truth",
            chunk_id="main",
            score=1.0,
            rank=rank,
            path="sql",
        )
        if page_slug not in by_page:
            by_page[page_slug] = FusedResult(
                page_slug=page_slug,
                chunks=[hit],
                rrf_score=1.0 / rank,
                final_rank=0,
            )
        else:
            by_page[page_slug].chunks.append(hit)
            by_page[page_slug].rrf_score += 1.0 / rank

    results = sorted(by_page.values(), key=lambda result: (-result.rrf_score, result.page_slug))
    for final_rank, result in enumerate(results, start=1):
        result.final_rank = final_rank
    return results


def _canonical_page_slug(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    subject = str(row["subject"])
    subject_slug = _entity_page_slug(conn, subject)
    if subject_slug is not None:
        return subject_slug

    if str(row["object_type"]) == "entity":
        object_slug = _entity_page_slug(conn, str(row["object"]))
        if object_slug is not None:
            return object_slug

    return subject


def _entity_page_slug(conn: sqlite3.Connection, entity_ref: str) -> str | None:
    row = conn.execute(
        "SELECT id, page_path FROM entities WHERE id = ?",
        (entity_ref,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id, page_path FROM entities WHERE title = ? COLLATE NOCASE ORDER BY id LIMIT 1",
            (entity_ref,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT entities.id, entities.page_path
            FROM entity_aliases
            JOIN entities ON entities.id = entity_aliases.entity_id
            WHERE entity_aliases.alias = ? COLLATE NOCASE
            ORDER BY entities.id
            LIMIT 1
            """,
            (entity_ref,),
        ).fetchone()
    if row is None:
        return None

    page_path = row["page_path"]
    if isinstance(page_path, str) and page_path:
        return page_path.rsplit("/", 1)[-1].removesuffix(".md")
    return str(row["id"])
