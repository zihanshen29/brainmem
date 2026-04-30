from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ImportFileKind = Literal["md", "txt", "pdf", "jsonl"]
ImportFileStatus = Literal["pending", "extracted", "ingested", "failed", "skipped"]
ImportJobStatus = Literal["running", "completed", "failed", "paused"]


class ImportFile(BaseModel):
    """A source file registered against a bulk import job."""

    job_id: str
    file_path: str
    file_hash: str
    kind: ImportFileKind
    status: ImportFileStatus
    laundry_path: str | None = None
    error: str | None = None
    processed_at: datetime | None = None


class ImportJob(BaseModel):
    """A persisted bulk import job."""

    id: str
    source_path: str
    started_at: datetime
    finished_at: datetime | None = None
    status: ImportJobStatus
    total_files: int
    processed_files: int = 0
    failed_files: int = 0
    estimated_tokens: int | None = None
    estimated_usd: float | None = None
    actual_tokens: int = 0
    metadata: dict[str, object] = Field(default_factory=dict)


class CostEstimate(BaseModel):
    """A dry-run estimate for a bulk import."""

    total_files: int
    by_kind: dict[ImportFileKind, int]
    estimated_extraction_tokens: int
    estimated_embedding_tokens: int
    estimated_extraction_usd: float
    estimated_embedding_usd: float
    estimated_total_usd: float
