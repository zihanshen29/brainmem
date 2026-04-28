import re
from datetime import datetime
from enum import StrEnum
from typing import Any

import ulid
from pydantic import BaseModel, ConfigDict, Field, field_validator

ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class EventKind(StrEnum):
    """Kinds of append-only events in the ledger."""

    RAW_IMPORTED = "raw_imported"
    LAUNDRY_INGESTED = "laundry_ingested"
    NOTE_APPENDED = "note_appended"
    AI_CHAT = "ai_chat"
    HUMAN_CHAT = "human_chat"
    REVIEW_DECIDED = "review_decided"
    PAGE_EDITED = "page_edited"
    REBUILD = "rebuild"


class Event(BaseModel):
    """Append-only source event stored in events.jsonl."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="ULID, monotonic and sortable")
    timestamp: datetime
    kind: EventKind
    source_ref: str = Field(..., min_length=1)
    raw_payload: str | None = None
    raw_payload_path: str | None = None
    extracted_facts: list[str] = Field(default_factory=list)
    affected_pages: list[str] = Field(default_factory=list)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_ulid(cls, value: str) -> str:
        """Require canonical 26-character uppercase ULID strings."""
        if not ULID_PATTERN.fullmatch(value):
            raise ValueError("event id must be a 26-character uppercase ULID")
        try:
            ulid.ULID.from_str(value)
        except ValueError as exc:
            raise ValueError("event id must be a valid ULID") from exc
        return value
