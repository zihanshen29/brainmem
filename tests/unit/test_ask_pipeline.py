from __future__ import annotations

import importlib
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
    EmbeddingChunk,
    Entity,
    EntityAliasSource,
    EntityType,
    Frontmatter,
    Page,
    PageType,
    RetrievalHit,
    Tier,
)
from brain.pages import write_page
from brain.pipeline.ask import ask

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"
OLD_ZHANG_ALIAS = "\N{CJK UNIFIED IDEOGRAPH-8001}\N{CJK UNIFIED IDEOGRAPH-5F20}"


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
    assert result.effective_mode == "keyword-only"
    assert result.warnings
    assert result.results[0].page_type is PageType.PROJECT
    assert result.results[0].compiled_truth.startswith("Computer vision coursework")


def test_keyword_only_mode_does_not_need_embeddings(brain_root: Path) -> None:
    result = ask(brain_root, "computer vision", top=3, mode="keyword-only")

    assert result.mode == "keyword-only"
    assert result.effective_mode == "keyword-only"
    assert result.warnings == []
    assert result.results[0].slug == "cv-coursework"


def test_hybrid_uses_vector_hits_when_embeddings_are_available(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain.db.embeddings import upsert_embedding
    from brain.llm import embedding as embedding_module

    ask_pipeline = importlib.import_module("brain.pipeline.ask")

    class FakeEmbeddingClient:
        def __init__(self, config: object) -> None:
            self.last_call_tokens = 0

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 1536 for _ in texts]

    with connect(brain_root / "brain.db") as conn:
        upsert_embedding(
            conn,
            EmbeddingChunk(
                page_slug="zhang-san",
                chunk_kind="compiled_truth",
                chunk_id="main",
                text="Zhang semantic text",
                text_preview="Zhang semantic text",
            ),
            "hash-zhang",
            [0.0] * 1536,
            "text-embedding-3-small",
        )
        upsert_embedding(
            conn,
            EmbeddingChunk(
                page_slug="cv-coursework",
                chunk_kind="compiled_truth",
                chunk_id="main",
                text="Coursework semantic text",
                text_preview="Coursework semantic text",
            ),
            "hash-cv",
            [1.0] * 1536,
            "text-embedding-3-small",
        )
        conn.commit()

    monkeypatch.setattr(embedding_module, "OpenAICompatibleEmbeddingClient", FakeEmbeddingClient)
    monkeypatch.setattr(ask_pipeline, "_vector_path_available", lambda conn: True)

    result = ask(brain_root, "semantic-only query", top=2, debug=True)

    assert result.effective_mode == "hybrid"
    assert result.results[0].slug == "zhang-san"
    assert result.trace is not None
    assert result.trace.vector[0]["page_slug"] == "zhang-san"
    assert result.trace.rrf


def test_alias_query_uses_entity_match_and_backlink_boost(brain_root: Path) -> None:
    result = ask(brain_root, OLD_ZHANG_ALIAS, top=5, show_sql=True)

    slugs = [page.slug for page in result.results]
    assert "zhang-san" in slugs
    assert "cv-coursework" in slugs
    assert result.trace is not None
    assert result.trace.matched_entities == ["zhang-san"]
    assert result.trace.boosted_pages == ["cv-coursework"]


def test_semantic_mode_uses_vector_path(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ask_pipeline = importlib.import_module("brain.pipeline.ask")

    monkeypatch.setattr(
        ask_pipeline,
        "_vector_hits",
        lambda conn, query, top, embedding_config: [
            RetrievalHit(
                page_slug="cv-coursework",
                chunk_kind="compiled_truth",
                chunk_id="main",
                score=0.02,
                rank=1,
                path="vector",
            )
        ],
    )

    result = ask(brain_root, "semantic query", mode="semantic", debug=True)

    assert result.effective_mode == "semantic"
    assert [page.slug for page in result.results] == ["cv-coursework"]
    assert result.trace is not None
    assert result.trace.vector[0]["path"] == "vector"


def test_sql_mode_uses_entity_match(brain_root: Path) -> None:
    result = ask(brain_root, "list Zhang San 2026", mode="sql", debug=True)

    assert result.effective_mode == "sql"
    assert [page.slug for page in result.results] == ["zhang-san"]
    assert result.trace is not None
    assert result.trace.sql_path


def test_default_hybrid_structured_query_uses_sql_direct_shortcut(brain_root: Path) -> None:
    result = ask(brain_root, "list Zhang San 2026", debug=True)

    assert result.mode == "hybrid"
    assert result.effective_mode == "sql"
    assert result.warnings == []
    assert [page.slug for page in result.results] == ["zhang-san"]
    assert result.trace is not None
    assert result.trace.classifier == "structured"
    assert result.trace.sql_path
    assert result.trace.vector == []


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
            sources=["docs/cli.md"],
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
        conn.execute(
            """
            INSERT INTO facts (
                subject, predicate, object, object_type, asserted_at, source_event, source_ref, confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "zhang-san",
                "reviewed",
                "computer vision coursework",
                "text",
                "2026-04-28T12:00:00+00:00",
                SECOND_ULID,
                "pages/entities/zhang-san.md",
                0.9,
            ),
        )
        conn.commit()
