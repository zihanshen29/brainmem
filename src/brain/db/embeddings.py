import sqlite3

from brain.exceptions import DBError
from brain.models import EmbeddingChunk, EmbeddingRecord, RetrievalHit


def _serialize_vector(vector: list[float]) -> bytes:
    """Serialize a float vector using sqlite-vec's Python adapter."""
    try:
        import sqlite_vec
    except ImportError as exc:
        raise DBError("sqlite-vec is required for embedding storage") from exc
    return sqlite_vec.serialize_float32(vector)


def _record_from_row(row: sqlite3.Row) -> EmbeddingRecord:
    """Build an embedding model from a SQLite row."""
    return EmbeddingRecord(
        rowid=row["rowid"],
        page_slug=row["page_slug"],
        chunk_kind=row["chunk_kind"],
        chunk_id=row["chunk_id"],
        content_hash=row["content_hash"],
        model=row["model"],
        text_preview=row["text_preview"],
        created_at=row["created_at"],
    )


def upsert_embedding(
    conn: sqlite3.Connection,
    chunk: EmbeddingChunk,
    content_hash: str,
    vector: list[float],
    model: str,
) -> int:
    """Insert or replace the current embedding for a chunk."""
    existing = conn.execute(
        """
        SELECT rowid FROM embedding_index
        WHERE page_slug = ? AND chunk_kind = ? AND chunk_id = ?
        """,
        (chunk.page_slug, chunk.chunk_kind, chunk.chunk_id),
    ).fetchone()
    if existing is not None:
        delete_embedding(conn, existing["rowid"])

    cursor = conn.execute(
        "INSERT INTO embeddings (embedding) VALUES (?)",
        (_serialize_vector(vector),),
    )
    rowid = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO embedding_index (
            rowid, page_slug, chunk_kind, chunk_id, content_hash, model, text_preview, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            rowid,
            chunk.page_slug,
            chunk.chunk_kind,
            chunk.chunk_id,
            content_hash,
            model,
            chunk.text_preview,
        ),
    )
    return rowid


def delete_embedding(conn: sqlite3.Connection, rowid: int) -> None:
    """Delete a vector row and its metadata."""
    conn.execute("DELETE FROM embeddings WHERE rowid = ?", (rowid,))
    conn.execute("DELETE FROM embedding_index WHERE rowid = ?", (rowid,))


def find_embeddings_for_page(
    conn: sqlite3.Connection,
    page_slug: str,
) -> list[EmbeddingRecord]:
    """Return embedding metadata rows for a page."""
    rows = conn.execute(
        """
        SELECT * FROM embedding_index
        WHERE page_slug = ?
        ORDER BY chunk_kind, chunk_id
        """,
        (page_slug,),
    ).fetchall()
    return [_record_from_row(row) for row in rows]


def vector_search(
    conn: sqlite3.Connection,
    query_vector: list[float],
    top_k: int,
) -> list[RetrievalHit]:
    """Search embeddings by vector distance."""
    rows = conn.execute(
        """
        SELECT
            embedding_index.page_slug,
            embedding_index.chunk_kind,
            embedding_index.chunk_id,
            embeddings.distance
        FROM embeddings
        JOIN embedding_index ON embedding_index.rowid = embeddings.rowid
        WHERE embeddings.embedding MATCH ? AND k = ?
        ORDER BY embeddings.distance
        """,
        (_serialize_vector(query_vector), top_k),
    ).fetchall()
    return [
        RetrievalHit(
            page_slug=row["page_slug"],
            chunk_kind=row["chunk_kind"],
            chunk_id=row["chunk_id"],
            score=row["distance"],
            rank=index,
            path="vector",
        )
        for index, row in enumerate(rows, start=1)
    ]
