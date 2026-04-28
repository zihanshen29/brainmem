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


class Tier(IntEnum):
    """Entity importance tier."""

    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3


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

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        """Require lowercase ASCII slugs separated by hyphens."""
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("slug must be lowercase ASCII words separated by hyphens")
        return value

    @model_validator(mode="after")
    def validate_entity_tier(self) -> "Frontmatter":
        """Entity pages must declare a tier."""
        if self.type is PageType.ENTITY and self.tier is None:
            raise ValueError("entity pages must include tier")
        return self


class Page(BaseModel):
    """Parsed markdown page with frontmatter and canonical sections."""

    model_config = ConfigDict(extra="forbid")

    frontmatter: Frontmatter
    compiled_truth: str
    timeline: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
