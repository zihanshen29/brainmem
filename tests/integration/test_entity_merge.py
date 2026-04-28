from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import brain.pipeline.entity_merge as entity_merge_pipeline
from brain.cli.init import init_brain
from brain.cli.main import app
from brain.db.connection import connect
from brain.models import Frontmatter, Page, PageType, Tier
from brain.pages import parse_page, write_page

runner = CliRunner()
ALICE_EVENT = "01KQA8R9KVCG906A0203VYEQF7"
ALLY_EVENT = "01KQA8VZMXBAV7AKF5JFB4KQ9C"
PROJECT_EVENT = "01KQA8YFYZZKK3KVCB3E2YQ2ZW"


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


@pytest.fixture()
def brain_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    monkeypatch.setattr(
        entity_merge_pipeline.llm_client,
        "rewrite_compiled_truth",
        lambda timeline, current_truth: f"rewritten {len(timeline)} from {current_truth}",
    )
    return root


def test_entity_merge_happy_path_updates_db_pages_links_and_index(brain_root: Path) -> None:
    _seed_merge_fixture(brain_root)

    report = entity_merge_pipeline.merge_entities(brain_root, "alice", "ally", auto_commit=False)

    assert report.canonical == "alice"
    assert report.loser == "ally"
    assert not (brain_root / "pages" / "entities" / "ally.md").exists()

    entities = _rows(brain_root, "SELECT id, tier, mention_count FROM entities ORDER BY id")
    assert [row["id"] for row in entities] == ["alice"]
    assert entities[0]["tier"] == 1

    aliases = _rows(
        brain_root,
        "SELECT alias, entity_id FROM entity_aliases ORDER BY alias",
    )
    assert {tuple(row) for row in aliases} == {
        ("A.", "alice"),
        ("Al", "alice"),
        ("Ally", "alice"),
        ("ally", "alice"),
    }

    facts = _rows(
        brain_root,
        "SELECT subject, object, object_type FROM facts ORDER BY id",
    )
    assert [tuple(row) for row in facts] == [
        ("alice", "maintainer", "literal"),
        ("project-brain", "alice", "entity"),
    ]

    proposals = _rows(brain_root, "SELECT entity_id FROM tier_proposals")
    assert [row["entity_id"] for row in proposals] == ["alice"]
    assert _scalar(brain_root, "SELECT COUNT(*) FROM backlinks WHERE to_entity = 'ally'") == 0
    assert _scalar(brain_root, "SELECT COUNT(*) FROM backlinks WHERE to_entity = 'alice'") >= 1

    page = parse_page(brain_root / "pages" / "entities" / "alice.md")
    assert page.frontmatter.aliases == ["Al", "A.", "Ally", "ally"]
    assert page.frontmatter.tier == Tier.TIER_1
    assert page.timeline == [
        f"- 2026-04-01 [event:{ALICE_EVENT}]: Alice founded [[project-brain]].",
        f"- 2026-04-02 [event:{ALLY_EVENT}]: Ally joined [[project-brain]].",
    ]
    assert page.sources == ["events/alice.jsonl", "events/ally.jsonl"]
    assert page.compiled_truth == "rewritten 2 from Alice truth"

    project_text = (brain_root / "pages" / "projects" / "brain.md").read_text(encoding="utf-8")
    assert "[[alice|Ally display]]" in project_text
    assert "[[ally" not in project_text
    assert "- [Alice](entities/alice.md)" in (brain_root / "pages" / "index.md").read_text(
        encoding="utf-8"
    )
    assert "ally.md" not in (brain_root / "pages" / "index.md").read_text(encoding="utf-8")


def test_entity_merge_auto_commit_includes_rewritten_external_pages(brain_root: Path) -> None:
    _seed_merge_fixture(brain_root)

    report = entity_merge_pipeline.merge_entities(brain_root, "alice", "ally")

    assert report.committed is True
    assert _git_output(brain_root, "status", "--short") == ""
    committed_paths = _git_output(
        brain_root,
        "show",
        "--name-only",
        "--format=",
        "HEAD",
    ).splitlines()
    assert "pages/projects/brain.md" in committed_paths
    assert "pages/entities/alice.md" in committed_paths
    assert "pages/index.md" in committed_paths
    assert "brain.db" in committed_paths
    assert "[[alice|Ally display]]" in (
        brain_root / "pages" / "projects" / "brain.md"
    ).read_text(encoding="utf-8")


def test_entity_merge_into_b_keeps_second_slug(brain_root: Path) -> None:
    _seed_merge_fixture(brain_root)

    entity_merge_pipeline.merge_entities(brain_root, "alice", "ally", into="b", auto_commit=False)

    assert not (brain_root / "pages" / "entities" / "alice.md").exists()
    assert (brain_root / "pages" / "entities" / "ally.md").exists()
    assert _scalar(brain_root, "SELECT COUNT(*) FROM entities WHERE id = 'ally'") == 1
    assert _scalar(brain_root, "SELECT COUNT(*) FROM entities WHERE id = 'alice'") == 0


def test_entity_merge_alias_conflict_raises_and_keeps_loser_page(brain_root: Path) -> None:
    _seed_merge_fixture(brain_root)
    _write_page(
        brain_root,
        "entities/charlie.md",
        _entity_page(slug="charlie", title="Charlie", aliases=["Other"]),
    )
    with connect(brain_root / "brain.db") as conn:
        conn.execute(
            """
            INSERT INTO entities (id, type, title, page_path, tier, mention_count, first_seen, last_seen, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "charlie",
                "person",
                "Charlie",
                "pages/entities/charlie.md",
                3,
                0,
                _utc(1).isoformat(),
                _utc(1).isoformat(),
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO entity_aliases (alias, entity_id, source) VALUES (?, ?, ?)",
            ("Ally", "charlie", "frontmatter"),
        )
        conn.commit()

    with pytest.raises(Exception, match="belongs to another entity"):
        entity_merge_pipeline.merge_entities(brain_root, "alice", "ally", auto_commit=False)

    assert (brain_root / "pages" / "entities" / "ally.md").exists()
    assert _scalar(brain_root, "SELECT COUNT(*) FROM entities WHERE id = 'ally'") == 1


def test_entity_merge_write_page_failure_leaves_db_and_loser_page_unchanged(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_merge_fixture(brain_root)
    before = _db_snapshot(brain_root)
    canonical_text = (brain_root / "pages" / "entities" / "alice.md").read_text(encoding="utf-8")
    loser_path = brain_root / "pages" / "entities" / "ally.md"

    def fail_write_page(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(entity_merge_pipeline, "write_page", fail_write_page)

    with pytest.raises(RuntimeError, match="write failed"):
        entity_merge_pipeline.merge_entities(brain_root, "alice", "ally", auto_commit=False)

    assert _db_snapshot(brain_root) == before
    assert loser_path.exists()
    assert (brain_root / "pages" / "entities" / "alice.md").read_text(encoding="utf-8") == canonical_text


def test_entity_merge_cli_confirmation_reject_exits_one(brain_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_merge_fixture(brain_root)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["entity", "merge", "alice", "ally"], input="n\n")

    assert result.exit_code == 1
    assert "entity merge cancelled" in result.stderr
    assert (brain_root / "pages" / "entities" / "ally.md").exists()


def test_entity_merge_cli_yes_happy_path(brain_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_merge_fixture(brain_root)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["entity", "merge", "alice", "ally", "--yes"])

    assert result.exit_code == 0
    assert "Entity merge summary:" in result.stdout
    assert "canonical=alice" in result.stdout
    assert not (brain_root / "pages" / "entities" / "ally.md").exists()


def test_entity_merge_cli_brain_error_outputs_stderr_and_exit_one(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain.cli import entity as entity_cli
    from brain.exceptions import BrainError

    def raise_brain_error(*_args: object, **_kwargs: object) -> None:
        raise BrainError("merge failed")

    monkeypatch.setattr(entity_cli, "_run_merge", raise_brain_error)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["entity", "merge", "alice", "ally", "--yes"])

    assert result.exit_code == 1
    assert "Error: merge failed" in result.stderr


def _seed_merge_fixture(brain_root: Path) -> None:
    _write_page(
        brain_root,
        "entities/alice.md",
        _entity_page(
            slug="alice",
            title="Alice",
            tier=Tier.TIER_2,
            aliases=["Al"],
            compiled_truth="Alice truth",
            timeline=[f"- 2026-04-01 [event:{ALICE_EVENT}]: Alice founded [[project-brain]]."],
            sources=["events/alice.jsonl"],
            created=_utc(1),
            updated=_utc(2),
        ),
    )
    _write_page(
        brain_root,
        "entities/ally.md",
        _entity_page(
            slug="ally",
            title="Ally",
            tier=Tier.TIER_1,
            aliases=["A."],
            compiled_truth="Ally truth",
            timeline=[f"- 2026-04-02 [event:{ALLY_EVENT}]: Ally joined [[project-brain]]."],
            sources=["events/ally.jsonl"],
            created=_utc(0),
            updated=_utc(3),
        ),
    )
    _write_page(
        brain_root,
        "projects/brain.md",
        _project_page(
            compiled_truth="[[ally|Ally display]] works with [[alice]].",
        ),
    )
    _seed_db(brain_root)


def _seed_db(brain_root: Path) -> None:
    with connect(brain_root / "brain.db") as conn:
        for entity_id, title, tier, path in [
            ("alice", "Alice", 2, "pages/entities/alice.md"),
            ("ally", "Ally", 1, "pages/entities/ally.md"),
        ]:
            conn.execute(
                """
                INSERT INTO entities (id, type, title, page_path, tier, mention_count, first_seen, last_seen, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    "person",
                    title,
                    path,
                    tier,
                    0,
                    _utc(1).isoformat(),
                    _utc(2).isoformat(),
                    "{}",
                ),
            )
        conn.execute(
            "INSERT INTO entity_aliases (alias, entity_id, source) VALUES (?, ?, ?)",
            ("Al", "alice", "frontmatter"),
        )
        conn.execute(
            "INSERT INTO entity_aliases (alias, entity_id, source) VALUES (?, ?, ?)",
            ("A.", "ally", "frontmatter"),
        )
        conn.execute(
            """
            INSERT INTO facts (subject, predicate, object, object_type, asserted_at, source_event, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("ally", "role", "maintainer", "literal", _utc(4).isoformat(), ALICE_EVENT, 0.9),
        )
        conn.execute(
            """
            INSERT INTO facts (subject, predicate, object, object_type, asserted_at, source_event, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("project-brain", "owner", "ally", "entity", _utc(4).isoformat(), ALLY_EVENT, 0.9),
        )
        conn.execute(
            """
            INSERT INTO backlinks (from_page, to_entity, relation, line_number, extracted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("project-brain", "ally", "mentions", 5, _utc(4).isoformat()),
        )
        conn.execute(
            """
            INSERT INTO backlinks (from_page, to_entity, relation, line_number, extracted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("project-brain", "alice", "mentions", 5, _utc(4).isoformat()),
        )
        conn.execute(
            """
            INSERT INTO tier_proposals (entity_id, proposed_tier, current_tier, reason, proposed_at, review_file)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("ally", 1, 2, "important", _utc(4).isoformat(), "review/ally.md"),
        )
        conn.commit()


def _entity_page(
    *,
    slug: str,
    title: str,
    tier: Tier = Tier.TIER_3,
    aliases: list[str] | None = None,
    compiled_truth: str = "truth",
    timeline: list[str] | None = None,
    sources: list[str] | None = None,
    created: datetime | None = None,
    updated: datetime | None = None,
) -> Page:
    return Page(
        frontmatter=Frontmatter(
            type=PageType.ENTITY,
            slug=slug,
            title=title,
            tier=tier,
            created=created or _utc(1),
            updated=updated or _utc(2),
            tags=["person"],
            aliases=aliases or [],
            external_ids={},
        ),
        compiled_truth=compiled_truth,
        timeline=timeline or [f"- 2026-04-01 [event:{ALICE_EVENT}]: Created page"],
        sources=sources or [],
    )


def _project_page(*, compiled_truth: str) -> Page:
    return Page(
        frontmatter=Frontmatter(
            type=PageType.PROJECT,
            slug="project-brain",
            title="Brain Project",
            tier=None,
            created=_utc(1),
            updated=_utc(2),
            tags=["project"],
            aliases=[],
            external_ids={},
        ),
        compiled_truth=compiled_truth,
        timeline=[f"- 2026-04-03 [event:{PROJECT_EVENT}]: Project mentions [[ally]]."],
        sources=[],
    )


def _write_page(brain_root: Path, relative: str, page: Page) -> Path:
    path = brain_root / "pages" / relative
    write_page(path, page)
    return path


def _rows(brain_root: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with connect(brain_root / "brain.db") as conn:
        return conn.execute(sql, params).fetchall()


def _scalar(brain_root: Path, sql: str, params: tuple[object, ...] = ()) -> Any:
    return _rows(brain_root, sql, params)[0][0]


def _git_output(brain_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(brain_root), *args],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
        check=True,
    )
    return result.stdout


def _db_snapshot(brain_root: Path) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "entities": [
            tuple(row)
            for row in _rows(
                brain_root,
                "SELECT id, type, title, page_path, tier, mention_count, first_seen, last_seen, metadata FROM entities ORDER BY id",
            )
        ],
        "aliases": [
            tuple(row)
            for row in _rows(
                brain_root,
                "SELECT alias, entity_id, source FROM entity_aliases ORDER BY alias, entity_id",
            )
        ],
        "facts": [
            tuple(row)
            for row in _rows(
                brain_root,
                "SELECT subject, predicate, object, object_type, asserted_at, source_event, confidence FROM facts ORDER BY id",
            )
        ],
        "backlinks": [
            tuple(row)
            for row in _rows(
                brain_root,
                "SELECT from_page, to_entity, relation, line_number, extracted_at FROM backlinks ORDER BY from_page, to_entity, relation",
            )
        ],
        "tier_proposals": [
            tuple(row)
            for row in _rows(
                brain_root,
                "SELECT entity_id, proposed_tier, current_tier, reason, proposed_at, review_file FROM tier_proposals ORDER BY id",
            )
        ],
    }


def _utc(day: int) -> datetime:
    return datetime(2026, 4, max(day, 1), 12, 0, tzinfo=UTC)
