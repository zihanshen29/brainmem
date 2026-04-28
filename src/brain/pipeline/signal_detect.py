import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from brain.models.entity import EntityType
from brain.models.fact import FactCandidate
from brain.models.page import PageType


class SignalEntity(BaseModel):
    """Candidate entity mention returned by signal detection."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    type: EntityType | None = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SignalExtraction(BaseModel):
    """Structured LLM output for a signal-detection pass."""

    model_config = ConfigDict(extra="forbid")

    entities: list[SignalEntity]
    facts: list[FactCandidate]
    timeline_summary: str = Field(..., min_length=1)
    suggested_page_type: PageType | None


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
