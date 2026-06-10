import json
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from brain.models.entity import EntityType
from brain.models.fact import FactCandidate
from brain.models.page import PageType


class SignalEntity(BaseModel):
    """Candidate entity mention returned by signal detection."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    type: EntityType | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_empty_metadata(cls, value: Any) -> dict[str, Any]:
        """Treat LLM null metadata as an empty metadata object."""
        if value is None:
            return {}
        return value


class ProcedureCandidate(BaseModel):
    """Candidate reusable procedure returned by signal detection."""

    model_config = ConfigDict(extra="forbid")

    suggested_slug: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("suggested_slug", "slug"),
    )
    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    steps: list[str] = Field(default_factory=list)
    source_event: str | None = None
    source_ref: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_empty_metadata(cls, value: Any) -> dict[str, Any]:
        """Treat LLM null metadata as an empty metadata object."""
        if value is None:
            return {}
        return value

    @property
    def slug(self) -> str:
        """Compatibility alias for older internal callers."""
        return self.suggested_slug


class SignalExtraction(BaseModel):
    """Structured LLM output for a signal-detection pass."""

    model_config = ConfigDict(extra="forbid")

    entities: list[SignalEntity] = Field(default_factory=list)
    facts: list[FactCandidate] = Field(default_factory=list)
    procedure_candidates: list[ProcedureCandidate] = Field(default_factory=list)
    timeline_summary: str = Field(..., min_length=1)
    suggested_page_type: PageType | None = None


def _build_signal_input(text: str, hint: dict[str, Any] | None) -> str:
    if hint is None:
        return text

    hint_json = json.dumps(hint, ensure_ascii=False, sort_keys=True)
    return "\n".join(
        [
            "Hint:",
            hint_json,
            "",
            "Input text:",
            text,
        ]
    )


def detect_signal(text: str, hint: dict[str, Any] | None = None) -> SignalExtraction:
    """Extract candidate entities and facts from text through the LLM client."""
    from brain.llm.client import extract_signal

    raw = extract_signal(_build_signal_input(text, hint))
    return SignalExtraction.model_validate(raw)
