from datetime import UTC, datetime

import pytest

from brain.exceptions import BrainError
from brain.models import Frontmatter, Page, PageType, Tier
from brain.pipeline.chunking import split_page_into_chunks

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"


def utc_datetime() -> datetime:
    return datetime(2026, 4, 30, 12, 0, tzinfo=UTC)


def sample_page(
    *,
    compiled_truth: str = "Current best understanding.",
    timeline: list[str] | None = None,
    sources: list[str] | None = None,
) -> Page:
    return Page(
        frontmatter=Frontmatter(
            type=PageType.ENTITY,
            slug="zhang-san",
            title="Zhang San",
            tier=Tier.TIER_2,
            created=utc_datetime(),
            updated=utc_datetime(),
            tags=["secret-tag"],
            aliases=["private-alias"],
            external_ids={"source": "frontmatter-only"},
        ),
        compiled_truth=compiled_truth,
        timeline=timeline or [],
        sources=sources or [],
    )


def test_compiled_truth_generates_main_chunk_with_title() -> None:
    page = sample_page(compiled_truth="Works on the brain project.")

    chunks = split_page_into_chunks(page, max_chars=100)

    assert len(chunks) == 1
    assert chunks[0].page_slug == "zhang-san"
    assert chunks[0].chunk_kind == "compiled_truth"
    assert chunks[0].chunk_id == "main"
    assert chunks[0].text == "Zhang San\n\nWorks on the brain project."
    assert chunks[0].text_preview == "Works on the brain project."


def test_stub_compiled_truth_does_not_generate_compiled_truth_chunk() -> None:
    page = sample_page(compiled_truth=" (stub - waiting for more evidence) ")

    chunks = split_page_into_chunks(page, max_chars=100)

    assert chunks == []


def test_timeline_entries_generate_timeline_chunks_with_event_ids() -> None:
    page = sample_page(
        compiled_truth="",
        timeline=[
            f"- 2026-04-29 [event:{VALID_ULID}]: Created entity page",
            f"- 2026-04-30 [event:{SECOND_ULID}]: Added follow-up",
        ],
    )

    chunks = split_page_into_chunks(page, max_chars=100)

    assert [chunk.chunk_kind for chunk in chunks] == ["timeline_entry", "timeline_entry"]
    assert [chunk.chunk_id for chunk in chunks] == [VALID_ULID, SECOND_ULID]
    assert chunks[0].text == "2026-04-29 - Zhang San: Created entity page"
    assert chunks[1].text == "2026-04-30 - Zhang San: Added follow-up"


def test_long_text_is_truncated_and_preview_is_limited_to_200_chars() -> None:
    long_truth = "a" * 300
    long_description = "b" * 300
    page = sample_page(
        compiled_truth=long_truth,
        timeline=[f"- 2026-04-30 [event:{VALID_ULID}]: {long_description}"],
    )

    chunks = split_page_into_chunks(page, max_chars=250)

    assert chunks[0].text == f"Zhang San\n\n{'a' * 250}"
    assert chunks[0].text_preview == "a" * 200
    assert chunks[1].text == f"2026-04-30 - Zhang San: {'b' * 250}"
    assert chunks[1].text_preview == "b" * 200


def test_sources_and_frontmatter_are_not_included_in_chunk_text() -> None:
    page = sample_page(
        compiled_truth="Visible compiled truth.",
        sources=["events.jsonl", "raw/private-note.md"],
    )

    chunks = split_page_into_chunks(page, max_chars=100)

    assert "events.jsonl" not in chunks[0].text
    assert "raw/private-note.md" not in chunks[0].text
    assert "secret-tag" not in chunks[0].text
    assert "private-alias" not in chunks[0].text
    assert "frontmatter-only" not in chunks[0].text


def test_max_chars_must_be_positive() -> None:
    page = sample_page()

    with pytest.raises(BrainError, match="max_chars must be positive"):
        split_page_into_chunks(page, max_chars=0)
