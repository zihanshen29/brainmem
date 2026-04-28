from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FactObjectType(StrEnum):
    """Supported object value kinds for structured facts."""

    ENTITY = "entity"
    LITERAL = "literal"
    DATE = "date"
    NUMBER = "number"


class Fact(BaseModel):
    """Structured bi-temporal fact stored in SQLite."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = Field(default=None, ge=1)
    subject: str = Field(..., min_length=1)
    predicate: str = Field(..., min_length=1)
    object: str = Field(..., min_length=1)
    object_type: FactObjectType
    valid_from: str | None = None
    valid_to: str | None = None
    asserted_at: datetime
    source_event: str = Field(..., min_length=1)
    source_ref: str | None = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    superseded_by: int | None = Field(default=None, ge=1)


class FactCandidate(BaseModel):
    """Candidate fact produced during signal detection before persistence."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(..., min_length=1)
    predicate: str = Field(..., min_length=1)
    object: str = Field(..., min_length=1)
    object_type: FactObjectType
    valid_from: str | None = None
    valid_to: str | None = None
    source_event: str = Field(..., min_length=1)
    source_ref: str | None = None
    confidence: float = Field(..., ge=0.0, le=1.0)
