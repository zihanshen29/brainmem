import sqlite3
from pathlib import Path

import pytest

from brain.db.embeddings import (
    delete_embedding,
    find_embeddings_for_page,
    upsert_embedding,
    vector_search,
)
from brain.db.migrations import init_db
from brain.models import EmbeddingChunk

pytest.importorskip("sqlite_vec")


DIMENSION = 1536


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "brain.db"
    init_db(db_path)
    from brain.db.connection import connect

    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def vector(value: float) -> list[float]:
    return [value] * DIMENSION


def sample_chunk(
    page_slug: str = "page-one",
    chunk_kind: str = "compiled_truth",
    chunk_id: str = "main",
) -> EmbeddingChunk:
    return EmbeddingChunk(
        page_slug=page_slug,
        chunk_kind=chunk_kind,
        chunk_id=chunk_id,
        text="Full text for embedding",
        text_preview="Full text for embedding",
    )


def test_upsert_and_lookup_round_trip(conn: sqlite3.Connection) -> None:
    rowid = upsert_embedding(conn, sample_chunk(), "hash-1", vector(0.1), "text-embedding-3-small")

    records = find_embeddings_for_page(conn, "page-one")

    assert len(records) == 1
    assert records[0].rowid == rowid
    assert records[0].page_slug == "page-one"
    assert records[0].chunk_kind == "compiled_truth"
    assert records[0].chunk_id == "main"
    assert records[0].content_hash == "hash-1"
    assert records[0].model == "text-embedding-3-small"


def test_upsert_replaces_existing_chunk(conn: sqlite3.Connection) -> None:
    first_rowid = upsert_embedding(conn, sample_chunk(), "hash-1", vector(0.1), "model-a")
    second_rowid = upsert_embedding(conn, sample_chunk(), "hash-2", vector(0.2), "model-a")

    records = find_embeddings_for_page(conn, "page-one")
    old_vec = conn.execute(
        "SELECT rowid FROM embeddings WHERE rowid = ?",
        (first_rowid,),
    ).fetchone()

    assert second_rowid != first_rowid
    assert old_vec is None
    assert len(records) == 1
    assert records[0].rowid == second_rowid
    assert records[0].content_hash == "hash-2"


def test_vector_search_returns_distance_sorted_results(conn: sqlite3.Connection) -> None:
    upsert_embedding(conn, sample_chunk("near"), "hash-near", vector(0.0), "model-a")
    upsert_embedding(conn, sample_chunk("far"), "hash-far", vector(1.0), "model-a")
    upsert_embedding(conn, sample_chunk("middle"), "hash-middle", vector(0.5), "model-a")

    hits = vector_search(conn, vector(0.1), 3)

    assert [hit.page_slug for hit in hits] == ["near", "middle", "far"]
    assert [hit.rank for hit in hits] == [1, 2, 3]
    assert all(hit.path == "vector" for hit in hits)
    assert [hit.score for hit in hits] == sorted(hit.score for hit in hits)


def test_dimension_mismatch_insert_fails(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.Error):
        upsert_embedding(conn, sample_chunk(), "hash-1", [0.1, 0.2], "model-a")


def test_delete_removes_vec_and_index_rows(conn: sqlite3.Connection) -> None:
    rowid = upsert_embedding(conn, sample_chunk(), "hash-1", vector(0.1), "model-a")

    delete_embedding(conn, rowid)

    index_row = conn.execute(
        "SELECT rowid FROM embedding_index WHERE rowid = ?",
        (rowid,),
    ).fetchone()
    vec_row = conn.execute("SELECT rowid FROM embeddings WHERE rowid = ?", (rowid,)).fetchone()

    assert index_row is None
    assert vec_row is None
