from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from brain.models.event import EventKind


class ExtractedDocument(BaseModel):
    """A markdown document produced from an imported source file."""

    title: str
    content: str
    metadata: dict[str, object] = Field(default_factory=dict)
    suggested_kind: EventKind = EventKind.RAW_IMPORTED


class Extractor(Protocol):
    """Extractor interface for one or more file kinds."""

    def can_handle(self, path: Path) -> bool:
        """Return whether this extractor can process path."""

    def extract(self, path: Path) -> list[ExtractedDocument]:
        """Extract one or more markdown documents from path."""

    def estimate_tokens(self, path: Path) -> int:
        """Estimate tokens without calling a model or remote API."""
