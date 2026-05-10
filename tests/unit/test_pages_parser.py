from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.exceptions import PageParseError
from brain.models import Frontmatter, Page, PageType, ProcedureStatus, Tier
from brain.pages import (
    TimelineEntry,
    append_log,
    append_timeline,
    parse_page,
    regenerate_index,
    update_compiled_truth,
    update_sources,
    write_page,
)

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"


def utc_datetime() -> datetime:
    return datetime(2026, 4, 28, 12, 0, tzinfo=UTC)


def sample_page() -> Page:
    return Page(
        frontmatter=Frontmatter(
            type=PageType.ENTITY,
            slug="zhang-san",
            title="Zhang San",
            tier=Tier.TIER_2,
            created=utc_datetime(),
            updated=utc_datetime(),
            tags=["people"],
            aliases=["zs"],
            external_ids={"github": "zhangsan"},
        ),
        compiled_truth="Current best understanding.\nStill true.",
        timeline=[f"- 2026-04-28 [event:{VALID_ULID}]: Created entity page"],
        sources=["events.jsonl"],
    )


def sample_procedure_page() -> Page:
    return Page(
        frontmatter=Frontmatter(
            type=PageType.PROCEDURE,
            slug="run-smoke-tests",
            title="Run smoke tests",
            created=utc_datetime(),
            updated=utc_datetime(),
            tags=["procedure"],
            status=ProcedureStatus.STABLE,
            success_count=3,
            fail_count=0,
            last_run=utc_datetime(),
        ),
        compiled_truth="Run the focused smoke test suite.",
        timeline=[f"- 2026-04-28 [event:{VALID_ULID}]: Procedure created"],
        sources=["README.md"],
    )


def write_sample_markdown(path: Path, *, include_sources: bool = True) -> None:
    sources = "\n---\n# Sources\n\n- events.jsonl" if include_sources else ""
    path.write_text(
        "\n".join(
            [
                "---",
                "type: entity",
                "slug: zhang-san",
                "title: Zhang San",
                "tier: 2",
                "created: '2026-04-28T12:00:00Z'",
                "updated: '2026-04-28T12:00:00Z'",
                "tags: [people]",
                "aliases: [zs]",
                "external_ids:",
                "  github: zhangsan",
                "---",
                "",
                "# Compiled truth",
                "",
                "Current best understanding.",
                "---",
                "# Timeline",
                "",
                f"- 2026-04-28 [event:{VALID_ULID}]: Created entity page",
            ]
        )
        + sources
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_procedure_markdown(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                "type: procedure",
                "slug: run-smoke-tests",
                "title: Run smoke tests",
                "created: '2026-04-28T12:00:00Z'",
                "updated: '2026-04-28T12:00:00Z'",
                "tags: [procedure]",
                "status: stable",
                "success_count: 3",
                "fail_count: 0",
                "last_run: '2026-04-28T12:00:00Z'",
                "---",
                "",
                "# Compiled truth",
                "",
                "Run the focused smoke test suite.",
                "---",
                "# Timeline",
                "",
                f"- 2026-04-28 [event:{VALID_ULID}]: Procedure created",
                "---",
                "# Sources",
                "",
                "- README.md",
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def test_parse_write_parse_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    page = sample_page()

    write_page(path, page)
    parsed = parse_page(path)
    write_page(path, parsed)

    assert parse_page(path) == page


def test_procedure_page_parse_write_parse_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "procedure.md"
    page = sample_procedure_page()

    write_page(path, page)
    parsed = parse_page(path)
    write_page(path, parsed)

    assert parse_page(path) == page


def test_append_timeline_preserves_procedure_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "procedure.md"
    write_procedure_markdown(path)
    before = path.read_text(encoding="utf-8")

    append_timeline(
        path,
        TimelineEntry(
            date="2026-04-29",
            event_id=SECOND_ULID,
            description="Ran successfully",
        ),
    )

    after = path.read_text(encoding="utf-8")
    assert parse_page(path).frontmatter.status is ProcedureStatus.STABLE
    assert f"- 2026-04-29 [event:{SECOND_ULID}]: Ran successfully" in after
    assert before.split("# Compiled truth")[0] == after.split("# Compiled truth")[0]


def test_append_timeline_preserves_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    write_sample_markdown(path)
    before = path.read_text(encoding="utf-8")

    append_timeline(
        path,
        TimelineEntry(
            date="2026-04-29",
            event_id=SECOND_ULID,
            description="Added follow-up",
        ),
    )

    after = path.read_text(encoding="utf-8")
    assert "# Compiled truth\n\nCurrent best understanding." in after
    assert "# Sources\n\n- events.jsonl" in after
    assert f"- 2026-04-29 [event:{SECOND_ULID}]: Added follow-up" in after
    assert before.split("# Timeline")[0] == after.split("# Timeline")[0]
    assert before.split("# Sources")[1] == after.split("# Sources")[1]


def test_update_compiled_truth_preserves_timeline(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    write_sample_markdown(path)
    original_timeline = path.read_text(encoding="utf-8").split("# Timeline", maxsplit=1)[1]

    update_compiled_truth(path, "Updated truth.")

    parsed = parse_page(path)
    assert parsed.compiled_truth == "Updated truth."
    assert path.read_text(encoding="utf-8").split("# Timeline", maxsplit=1)[1] == original_timeline


def test_missing_required_section_raises_page_parse_error(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    write_sample_markdown(path)
    text = path.read_text(encoding="utf-8").replace("# Timeline", "# Sources", 1)
    path.write_text(text, encoding="utf-8", newline="\n")

    with pytest.raises(PageParseError):
        parse_page(path)


def test_sources_section_is_optional(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    write_sample_markdown(path, include_sources=False)

    parsed = parse_page(path)

    assert parsed.sources == []


def test_write_page_adds_empty_sources_section(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    page = sample_page().model_copy(update={"sources": []})

    write_page(path, page)

    text = path.read_text(encoding="utf-8")
    assert "# Sources" in text
    assert parse_page(path) == page


def test_update_sources_creates_missing_section(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    write_sample_markdown(path, include_sources=False)

    update_sources(path, ["events.jsonl", "raw/example.md"])

    parsed = parse_page(path)
    assert parsed.sources == ["events.jsonl", "raw/example.md"]


def test_regenerate_index_and_append_log(tmp_path: Path) -> None:
    brain_root = tmp_path / "brain"
    page_path = brain_root / "pages" / "entities" / "zhang-san.md"
    write_page(page_path, sample_page())

    regenerate_index(brain_root)
    append_log(brain_root, "- 2026-04-28 ingest: 1 events processed")

    assert "- [Zhang San](entities/zhang-san.md)" in (
        brain_root / "pages" / "index.md"
    ).read_text(encoding="utf-8")
    assert (brain_root / "pages" / "log.md").read_text(encoding="utf-8") == (
        "- 2026-04-28 ingest: 1 events processed\n"
    )
