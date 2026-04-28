from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.cli.init import init_brain
from brain.db.backlinks import replace_backlinks_for_page
from brain.db.connection import connect
from brain.db.entities import add_alias, upsert_entity
from brain.exceptions import BrainError
from brain.models import (
    Backlink,
    Entity,
    EntityAliasSource,
    EntityType,
    Frontmatter,
    Page,
    PageType,
    Tier,
)
from brain.pages import write_page
from brain.pipeline.ask import ask

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"
OLD_ZHANG_ALIAS = "\N{CJK UNIFIED IDEOGRAPH-8001}\N{CJK UNIFIED IDEOGRAPH-5F20}"


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


@pytest.fixture()
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    _seed_pages(root)
    _seed_db(root)
    return root


def test_keyword_query_finds_expected_page_in_top_three(brain_root: Path) -> None:
    result = ask(brain_root, "computer vision", top=3)

    assert [page.slug for page in result.results][:1] == ["cv-coursework"]
    assert result.results[0].page_type is PageType.PROJECT
    assert result.results[0].compiled_truth.startswith("Computer vision coursework")


def test_alias_query_uses_entity_match_and_backlink_boost(brain_root: Path) -> None:
    result = ask(brain_root, OLD_ZHANG_ALIAS, top=5, show_sql=True)

    slugs = [page.slug for page in result.results]
    assert "zhang-san" in slugs
    assert "cv-coursework" in slugs
    assert result.trace is not None
    assert result.trace.matched_entities == ["zhang-san"]
    assert result.trace.boosted_pages == ["cv-coursework"]


def test_page_type_filter_and_top_limit(brain_root: Path) -> None:
    result = ask(brain_root, "computer vision zhang", top=1, page_type=PageType.ENTITY)

    assert len(result.results) == 1
    assert result.results[0].page_type is PageType.ENTITY
    assert result.results[0].slug == "zhang-san"


def test_empty_query_raises_brain_error(brain_root: Path) -> None:
    with pytest.raises(BrainError):
        ask(brain_root, "   ")


def test_explain_calls_llm_with_retrieved_page_dicts(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain.llm import client as llm_client

    calls: list[tuple[str, list[dict[str, object]]]] = []

    def fake_answer(query: str, pages: list[dict[str, object]]) -> llm_client.QuestionAnswer:
        calls.append((query, pages))
        return llm_client.QuestionAnswer(
            answer="Computer vision coursework involves Zhang San.",
            sources=["cv-coursework"],
        )

    monkeypatch.setattr(llm_client, "answer_question", fake_answer)

    result = ask(brain_root, "computer vision", explain=True)

    assert result.answer == "Computer vision coursework involves Zhang San."
    assert result.sources == ["cv-coursework"]
    assert calls[0][0] == "computer vision"
    assert calls[0][1][0]["slug"] == "cv-coursework"


def _seed_pages(root: Path) -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    _write_page(
        root / "pages" / "projects" / "cv-coursework.md",
        Page(
            frontmatter=Frontmatter(
                type=PageType.PROJECT,
                slug="cv-coursework",
                title="Computer Vision Coursework",
                created=now,
                updated=now,
                tags=[],
                aliases=[],
                external_ids={},
            ),
            compiled_truth="Computer vision coursework includes a baseline report with Zhang San.",
            timeline=[
                f"- 2026-04-27 [event:{VALID_ULID}]: Computer vision baseline was planned.",
                f"- 2026-04-28 [event:{SECOND_ULID}]: [[zhang-san]] reviewed the computer vision plan.",
            ],
            sources=["files/cli.md"],
        ),
    )
    _write_page(
        root / "pages" / "entities" / "zhang-san.md",
        Page(
            frontmatter=Frontmatter(
                type=PageType.ENTITY,
                slug="zhang-san",
                title="Zhang San",
                tier=Tier.TIER_2,
                created=now,
                updated=now,
                tags=[],
                aliases=[OLD_ZHANG_ALIAS],
                external_ids={},
            ),
            compiled_truth="Zhang San helps with computer vision reviews.",
            timeline=[f"- 2026-04-28 [event:{SECOND_ULID}]: Zhang San reviewed coursework."],
            sources=["events.jsonl"],
        ),
    )


def _write_page(path: Path, page: Page) -> None:
    write_page(path, page)


def _seed_db(root: Path) -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    with connect(root / "brain.db") as conn:
        upsert_entity(
            conn,
            Entity(
                id="zhang-san",
                type=EntityType.PERSON,
                title="Zhang San",
                page_path="pages/entities/zhang-san.md",
                tier=Tier.TIER_2,
                mention_count=1,
                first_seen=now,
                last_seen=now,
                metadata={},
            ),
        )
        add_alias(conn, OLD_ZHANG_ALIAS, "zhang-san", EntityAliasSource.MANUAL)
        replace_backlinks_for_page(
            conn,
            "cv-coursework",
            [
                Backlink(
                    from_page="cv-coursework",
                    to_entity="zhang-san",
                    relation="mentions",
                    line_number=1,
                    extracted_at=now,
                )
            ],
        )
        conn.commit()
