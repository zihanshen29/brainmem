from __future__ import annotations

import sqlite3
from typing import Protocol

from brain.db import embeddings as embedding_db
from brain.models import RetrievalHit


class QueryEmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def vector_search(
    conn: sqlite3.Connection,
    query: str,
    client: QueryEmbeddingClient,
    top: int = 50,
) -> list[RetrievalHit]:
    """Embed a query and delegate vector lookup to the DB layer."""
    if top <= 0:
        return []
    vectors = client.embed([query])
    if not vectors:
        return []
    return embedding_db.vector_search(conn, vectors[0], top)
