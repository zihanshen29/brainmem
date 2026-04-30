from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ChunkKind = Literal["compiled_truth", "timeline_entry"]


class EmbeddingChunk(BaseModel):
    """A text chunk ready to embed."""

    page_slug: str
    chunk_kind: ChunkKind
    chunk_id: str
    text: str
    text_preview: str


class EmbeddingRecord(BaseModel):
    """An embedding metadata row persisted in SQLite."""

    rowid: int
    page_slug: str
    chunk_kind: ChunkKind
    chunk_id: str
    content_hash: str
    model: str
    text_preview: str
    created_at: datetime


class RetrievalHit(BaseModel):
    """A retrieval hit from one search path."""

    page_slug: str
    chunk_kind: ChunkKind
    chunk_id: str
    score: float
    rank: int
    path: Literal["vector", "keyword", "sql"]


class FusedResult(BaseModel):
    """A final RRF result grouped by page."""

    page_slug: str
    chunks: list[RetrievalHit]
    rrf_score: float
    final_rank: int
