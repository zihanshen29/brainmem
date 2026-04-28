from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from brain.models import (
    Entity,
    EntityAlias,
    EntityAliasSource,
    EntityType,
    Event,
    EventKind,
    Fact,
    FactCandidate,
    FactObjectType,
    Frontmatter,
    Page,
    PageType,
    Tier,
)

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"


def utc_datetime() -> datetime:
    return datetime(2026, 4, 28, 12, 0, tzinfo=UTC)


def assert_round_trip(model):
    dumped = model.model_dump()
    assert type(model).model_validate(dumped) == model


def test_event_round_trip() -> None:
    event = Event(
        id=VALID_ULID,
        timestamp=utc_datetime(),
        kind=EventKind.LAUNDRY_INGESTED,
        source_ref="laundry/example.md",
        raw_payload="raw note",
        extracted_facts=["1"],
        affected_pages=["cv-coursework"],
        confidence=0.95,
        metadata={"source": "test"},
    )

    assert_round_trip(event)


def test_page_round_trip() -> None:
    page = Page(
        frontmatter=Frontmatter(
            type=PageType.ENTITY,
            slug="zhang-san",
            title="Zhang San",
            tier=Tier.TIER_2,
            created=utc_datetime(),
            updated=utc_datetime(),
            tags=["people"],
            aliases=["老张"],
            external_ids={"github": "zhangsan"},
        ),
        compiled_truth="Current best understanding.",
        timeline=[f"- 2026-04-28 [event:{VALID_ULID}]: Created entity page"],
        sources=["events.jsonl"],
    )

    assert_round_trip(page)


def test_fact_round_trip() -> None:
    fact = Fact(
        id=1,
        subject="zihan",
        predicate="location",
        object="UK",
        object_type=FactObjectType.LITERAL,
        valid_from="2024-09-01",
        valid_to=None,
        asserted_at=utc_datetime(),
        source_event=VALID_ULID,
        source_ref="events.jsonl",
        confidence=0.9,
        superseded_by=None,
    )

    assert_round_trip(fact)


def test_fact_candidate_round_trip() -> None:
    candidate = FactCandidate(
        subject="zihan",
        predicate="works_on",
        object="brain",
        object_type=FactObjectType.ENTITY,
        valid_from="2026-04-28",
        source_event=VALID_ULID,
        source_ref="laundry/example.md",
        confidence=0.86,
    )

    assert_round_trip(candidate)


def test_entity_round_trip() -> None:
    entity = Entity(
        id="zhang-san",
        type=EntityType.PERSON,
        title="Zhang San",
        page_path="pages/entities/zhang-san.md",
        tier=Tier.TIER_3,
        mention_count=2,
        first_seen=utc_datetime(),
        last_seen=utc_datetime(),
        metadata={"origin": "test"},
    )

    assert_round_trip(entity)


def test_entity_alias_round_trip() -> None:
    alias = EntityAlias(
        alias="老张",
        entity_id="zhang-san",
        source=EntityAliasSource.MANUAL,
    )

    assert_round_trip(alias)


@pytest.mark.parametrize(
    ("model_class", "valid_data", "missing_field"),
    [
        (
            Event,
            {
                "id": VALID_ULID,
                "timestamp": utc_datetime(),
                "kind": EventKind.REBUILD,
                "source_ref": "cli",
            },
            "source_ref",
        ),
        (
            Page,
            {
                "frontmatter": {
                    "type": PageType.PROJECT,
                    "slug": "brain",
                    "title": "Brain",
                    "created": utc_datetime(),
                    "updated": utc_datetime(),
                },
                "compiled_truth": "Project summary.",
            },
            "frontmatter",
        ),
        (
            Fact,
            {
                "subject": "zihan",
                "predicate": "location",
                "object": "UK",
                "object_type": FactObjectType.LITERAL,
                "asserted_at": utc_datetime(),
                "source_event": VALID_ULID,
                "confidence": 0.9,
            },
            "subject",
        ),
        (
            FactCandidate,
            {
                "subject": "zihan",
                "predicate": "location",
                "object": "UK",
                "object_type": FactObjectType.LITERAL,
                "source_event": VALID_ULID,
                "confidence": 0.9,
            },
            "predicate",
        ),
        (
            Entity,
            {
                "id": "zihan",
                "type": EntityType.PERSON,
                "title": "Zihan",
                "first_seen": utc_datetime(),
                "last_seen": utc_datetime(),
            },
            "title",
        ),
        (
            EntityAlias,
            {
                "alias": "Z",
                "entity_id": "zihan",
                "source": EntityAliasSource.MANUAL,
            },
            "alias",
        ),
    ],
)
def test_missing_required_fields_raise_validation_error(
    model_class, valid_data: dict, missing_field: str
) -> None:
    data = valid_data.copy()
    data.pop(missing_field)

    with pytest.raises(ValidationError):
        model_class.model_validate(data)


@pytest.mark.parametrize(
    "event_id",
    [
        "not-a-ulid",
        "01kqa8r9kvcg906a0203vyeqf7",
        "01KQA8R9KVCG906A0203VYEQF",
    ],
)
def test_event_id_must_be_valid_canonical_ulid(event_id: str) -> None:
    with pytest.raises(ValidationError):
        Event(
            id=event_id,
            timestamp=utc_datetime(),
            kind=EventKind.RAW_IMPORTED,
            source_ref="raw/example.md",
        )


def test_entity_page_frontmatter_requires_tier() -> None:
    with pytest.raises(ValidationError):
        Frontmatter(
            type=PageType.ENTITY,
            slug="zihan",
            title="Zihan",
            created=utc_datetime(),
            updated=utc_datetime(),
        )


def test_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Event(
            id=VALID_ULID,
            timestamp=utc_datetime(),
            kind=EventKind.RAW_IMPORTED,
            source_ref="raw/example.md",
            unexpected=True,
        )
