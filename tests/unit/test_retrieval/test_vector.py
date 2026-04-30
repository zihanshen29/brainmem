import sqlite3

from brain.models import RetrievalHit
from brain.pipeline.retrieval import vector


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1, 0.2, 0.3]]


def test_vector_search_embeds_query_and_passes_through_db_results(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    client = FakeClient()
    expected = [
        RetrievalHit(
            page_slug="target",
            chunk_kind="compiled_truth",
            chunk_id="main",
            score=0.12,
            rank=1,
            path="vector",
        )
    ]
    calls: list[tuple[sqlite3.Connection, list[float], int]] = []

    def fake_db_search(db_conn: sqlite3.Connection, query_vector: list[float], top_k: int):
        calls.append((db_conn, query_vector, top_k))
        return expected

    monkeypatch.setattr(vector.embedding_db, "vector_search", fake_db_search)

    assert vector.vector_search(conn, "find target", client, top=7) == expected
    assert client.calls == [["find target"]]
    assert calls == [(conn, [0.1, 0.2, 0.3], 7)]
