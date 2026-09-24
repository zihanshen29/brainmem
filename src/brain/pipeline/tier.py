from __future__ import annotations

import re
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from brain.config import Config
from brain.db.entities import get_entity
from brain.models import Tier
from brain.pipeline._config import resolve_pipeline_config


class TierProposal(BaseModel):
    """Pure proposal for an entity tier upgrade."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(..., min_length=1)
    current_tier: Tier
    proposed_tier: Tier
    reason: str = Field(..., min_length=1)
    mention_count: int = Field(..., ge=0)


def check_tier_upgrade(
    conn: sqlite3.Connection,
    entity_id: str,
    config: Config | None = None,
) -> TierProposal | None:
    """Return a tier upgrade proposal without writing review or DB rows."""
    entity = get_entity(conn, entity_id)
    if entity is None:
        return None
    if entity.metadata.get("tier_pinned"):
        return None
    rejected_count = entity.metadata.get("tier_rejected_at_count")
    if rejected_count is None:
        row = conn.execute("SELECT reason FROM tier_proposals WHERE entity_id = ? AND decision = 'rejected' ORDER BY id DESC LIMIT 1", (entity_id,)).fetchone()
        match = re.search(r"mention_count (\d+)", row[0]) if row else None
        if match:
            rejected_count = int(match[1])
    if isinstance(rejected_count, int) and entity.mention_count < rejected_count + max(10, (rejected_count + 1) // 2):
        return None

    resolved_config = resolve_pipeline_config(config)
    if (
        entity.tier is Tier.TIER_3
        and entity.mention_count >= resolved_config.tier.tier2_threshold
    ):
        return _proposal(entity.id, entity.tier, Tier.TIER_2, entity.mention_count)

    if (
        entity.tier is Tier.TIER_2
        and entity.mention_count >= resolved_config.tier.tier1_threshold
    ):
        return _proposal(entity.id, entity.tier, Tier.TIER_1, entity.mention_count)

    return None


def _proposal(
    entity_id: str,
    current_tier: Tier,
    proposed_tier: Tier,
    mention_count: int,
) -> TierProposal:
    return TierProposal(
        entity_id=entity_id,
        current_tier=current_tier,
        proposed_tier=proposed_tier,
        reason=f"mention_count {mention_count} reached tier {int(proposed_tier)} threshold",
        mention_count=mention_count,
    )
