from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from brain.models.page import Tier


class EntityType(StrEnum):
    """Entity categories stored in the registry."""

    PERSON = "person"
    ORG = "org"
    CONCEPT = "concept"
    PROJECT = "project"
    EVENT = "event"
    PLACE = "place"


class EntityAliasSource(StrEnum):
    """Sources that can create entity aliases."""

    FRONTMATTER = "frontmatter"
    AUTO_DETECTED = "auto_detected"
    MANUAL = "manual"


class Entity(BaseModel):
    """Canonical entity registry row."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    type: EntityType
    title: str = Field(..., min_length=1)
    page_path: str | None = None
    tier: Tier = Tier.TIER_3
    mention_count: int = Field(0, ge=0)
    first_seen: datetime
    last_seen: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntityAlias(BaseModel):
    """Alias pointing to a canonical entity."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., min_length=1)
    entity_id: str = Field(..., min_length=1)
    source: EntityAliasSource
