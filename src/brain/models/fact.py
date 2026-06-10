import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FactObjectType(StrEnum):
    """Supported object value kinds for structured facts."""

    ENTITY = "entity"
    LITERAL = "literal"
    DATE = "date"
    NUMBER = "number"


_OBJECT_TYPE_ALIASES = {
    "person": FactObjectType.ENTITY,
    "people": FactObjectType.ENTITY,
    "org": FactObjectType.ENTITY,
    "organization": FactObjectType.ENTITY,
    "company": FactObjectType.ENTITY,
    "place": FactObjectType.ENTITY,
    "location": FactObjectType.ENTITY,
    "concept": FactObjectType.ENTITY,
    "project": FactObjectType.ENTITY,
    "event": FactObjectType.ENTITY,
    "experience": FactObjectType.ENTITY,
    "conversation": FactObjectType.ENTITY,
    "page": FactObjectType.ENTITY,
    "string": FactObjectType.LITERAL,
    "text": FactObjectType.LITERAL,
    "bool": FactObjectType.LITERAL,
    "boolean": FactObjectType.LITERAL,
    "datetime": FactObjectType.DATE,
    "time": FactObjectType.DATE,
    "integer": FactObjectType.NUMBER,
    "float": FactObjectType.NUMBER,
    "decimal": FactObjectType.NUMBER,
    "numeric": FactObjectType.NUMBER,
}


def normalize_fact_object_type(value: Any) -> Any:
    """Normalize common LLM semantic labels to storage object types."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        return _OBJECT_TYPE_ALIASES.get(normalized, normalized)
    return value


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

    @field_validator("object", mode="before")
    @classmethod
    def normalize_object(cls, value: Any) -> Any:
        return normalize_fact_object(value)

    @field_validator("object_type", mode="before")
    @classmethod
    def normalize_object_type(cls, value: Any) -> Any:
        return normalize_fact_object_type(value)


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

    @field_validator("object", mode="before")
    @classmethod
    def normalize_object(cls, value: Any) -> Any:
        return normalize_fact_object(value)

    @field_validator("object_type", mode="before")
    @classmethod
    def normalize_object_type(cls, value: Any) -> Any:
        return normalize_fact_object_type(value)


def normalize_fact_object(value: Any) -> Any:
    """Stringify scalar LLM fact objects without hiding structured mistakes."""
    if isinstance(value, str) or value is None:
        return value
    if isinstance(value, int | float | bool):
        return json.dumps(value, ensure_ascii=False)
    return value
