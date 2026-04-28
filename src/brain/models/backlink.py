from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Backlink(BaseModel):
    """Typed link extracted from a markdown page."""

    model_config = ConfigDict(extra="forbid")

    from_page: str = Field(..., min_length=1)
    to_entity: str = Field(..., min_length=1)
    relation: str = Field(..., min_length=1)
    line_number: int | None = Field(default=None, ge=1)
    extracted_at: datetime
