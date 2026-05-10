import re
from datetime import datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class PageType(StrEnum):
    """Supported markdown wiki page types."""

    ENTITY = "entity"
    PROJECT = "project"
    CONCEPT = "concept"
    EVENT = "event"
    EXPERIENCE = "experience"
    CONVERSATION = "conversation"
    PROCEDURE = "procedure"


class Tier(IntEnum):
    """Entity importance tier."""

    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3


class ProcedureStatus(StrEnum):
    """Lifecycle status for procedure pages."""

    RAW = "raw"
    TESTED = "tested"
    STABLE = "stable"


class Frontmatter(BaseModel):
    """Structured YAML frontmatter for a markdown page."""

    model_config = ConfigDict(extra="forbid")

    type: PageType
    slug: str
    title: str = Field(..., min_length=1)
    tier: Tier | None = None
    created: datetime
    updated: datetime
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    external_ids: dict[str, str] = Field(default_factory=dict)
    status: ProcedureStatus | None = None
    success_count: int | None = None
    fail_count: int | None = None
    last_run: datetime | None = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        """Require lowercase ASCII slugs separated by hyphens."""
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("slug must be lowercase ASCII words separated by hyphens")
        return value

    @model_validator(mode="after")
    def validate_page_type_fields(self) -> "Frontmatter":
        """Validate page-type-specific frontmatter fields."""
        if self.type is PageType.ENTITY and self.tier is None:
            raise ValueError("entity pages must include tier")
        procedure_fields = {
            "status": self.status,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "last_run": self.last_run,
        }
        if self.type is PageType.PROCEDURE:
            missing = [
                name
                for name in ("status", "success_count", "fail_count")
                if procedure_fields[name] is None
            ]
            if missing:
                raise ValueError(
                    "procedure pages must include status, success_count, and fail_count"
                )
            for name in ("success_count", "fail_count"):
                count = procedure_fields[name]
                if count is not None and count < 0:
                    raise ValueError(f"{name} must be non-negative")
        elif any(value is not None for value in procedure_fields.values()):
            raise ValueError("procedure-only fields are only allowed on procedure pages")
        return self


class Page(BaseModel):
    """Parsed markdown page with frontmatter and canonical sections."""

    model_config = ConfigDict(extra="forbid")

    frontmatter: Frontmatter
    compiled_truth: str
    timeline: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
