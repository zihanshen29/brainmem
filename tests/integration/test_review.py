from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from brain.cli import review as review_cli
from brain.cli.main import app
from brain.db.connection import connect
from brain.db.entities import upsert_entity
from brain.db.facts import add_fact
from brain.db.tier import propose_tier
from brain.models import (
    Entity,
    EntityType,
    Fact,
    FactCandidate,
    FactObjectType,
    Frontmatter,
    Page,
    PageType,
    Tier,
)
from brain.pages import parse_page, write_page
from brain.paths import BrainPaths

try:
    from brain.pipeline import review as review_pipeline
    from brain.pipeline.review import (
        ReviewAction,
        ReviewKind,
        apply_pending,
        list_pending,
        parse_review_file,
        resolve_review_path,
    )
except ModuleNotFoundError as exc:
    if exc.name != "brain.pipeline.review":
        raise
    review_pipeline = None
    ReviewAction = None
    ReviewKind = None
    apply_pending = None
    list_pending = None
    parse_review_file = None
    resolve_review_path = None

requires_review_pipeline = pytest.mark.skipif(
    review_pipeline is None,
    reason="review pipeline is not available in this checkout",
)

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"

runner = CliRunner()


@requires_review_pipeline
def test_list_pending_filters_by_kind_and_resolves_prefix(brain_root: Path) -> None:
    low_path = _write_review(
        brain_root,
        "2026-04-28_001_low_confidence_fact",
        "low_confidence_fact",
        _low_confidence_body(_candidate()),
    )
    _write_review(
        brain_root,
        "2026-04-28_002_fact_conflict",
        "fact_conflict",
        _fact_conflict_body(_candidate(object_value="engineer"), []),
    )

    all_items = list_pending(brain_root)
    filtered = list_pending(brain_root, kind=ReviewKind.LOW_CONFIDENCE_FACT)

    assert [item.review_id for item in all_items] == [
        "2026-04-28_001_low_confidence_fact",
        "2026-04-28_002_fact_conflict",
    ]
    assert [item.review_id for item in filtered] == ["2026-04-28_001_low_confidence_fact"]
    assert resolve_review_path(brain_root, "2026-04-28_001") == low_path
    with pytest.raises(Exception, match="Ambiguous"):
        resolve_review_path(brain_root, "2026-04-28")


PARSE_DECISION_CASES = (
    [
        ("approve", ReviewAction.APPROVE),
        ("reject", ReviewAction.REJECT),
        ("defer", ReviewAction.DEFER),
        (None, ReviewAction.NONE),
    ]
    if ReviewAction is not None
    else [(None, None)]
)


@requires_review_pipeline
@pytest.mark.parametrize(
    ("checked", "expected"),
    PARSE_DECISION_CASES,
)
def test_parse_review_file_decision_checkbox(
    brain_root: Path,
    checked: str | None,
    expected: ReviewAction,
) -> None:
    path = _write_review(
        brain_root,
        "2026-04-28_001_low_confidence_fact",
        "low_confidence_fact",
        _low_confidence_body(_candidate()),
        checked=checked,
    )

    decision = parse_review_file(path)

    assert decision.kind is ReviewKind.LOW_CONFIDENCE_FACT
    assert decision.action is expected
    assert decision.candidate == _candidate()


@requires_review_pipeline
def test_apply_fact_conflict_adds_new_fact_and_supersedes_old(brain_root: Path) -> None:
    old_fact = _insert_fact(brain_root, object_value="designer")
    candidate = _candidate(object_value="engineer", valid_from="2026-04-28")
    _write_review(
        brain_root,
        "2026-04-28_001_fact_conflict",
        "fact_conflict",
        _fact_conflict_body(candidate, [old_fact]),
        checked="approve",
    )

    report = apply_pending(brain_root)

    facts = _rows(brain_root, "SELECT * FROM facts ORDER BY id")
    old_row = facts[0]
    new_row = facts[1]
    assert report.applied == 1
    assert report.archived == 1
    assert _git_log_messages(brain_root)[0] == "review: apply 1 decisions"
    assert new_row["object"] == "engineer"
    assert old_row["valid_to"] == "2026-04-28"
    assert old_row["superseded_by"] == new_row["id"]
    assert _archived_review(brain_root).exists()
    assert "status: approved" in _archived_review(brain_root).read_text(encoding="utf-8")


@requires_review_pipeline
def test_apply_low_confidence_approve_inserts_candidate_fact(brain_root: Path) -> None:
    _write_review(
        brain_root,
        "2026-04-28_001_low_confidence_fact",
        "low_confidence_fact",
        _low_confidence_body(_candidate()),
        checked="approve",
    )

    report = apply_pending(brain_root)

    facts = _rows(brain_root, "SELECT * FROM facts")
    assert report.applied == 1
    assert len(facts) == 1
    assert facts[0]["subject"] == "alice"
    assert facts[0]["predicate"] == "role"


@requires_review_pipeline
def test_apply_pending_isolates_bad_review_and_continues(brain_root: Path) -> None:
    bad_path = _write_review(
        brain_root,
        "2026-04-28_001_low_confidence_fact",
        "low_confidence_fact",
        _low_confidence_body(_candidate()),
    )
    bad_path.write_text(
        "\n".join(
            [
                "---",
                "review_id: 2026-04-28_001_low_confidence_fact",
                "kind: low_confidence_fact",
                "status: pending",
                "---",
                "",
                "# Bad review",
                "",
                "[x] launch",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    _write_review(
        brain_root,
        "2026-04-28_002_low_confidence_fact",
        "low_confidence_fact",
        _low_confidence_body(_candidate(object_value="artist")),
        checked="approve",
    )

    report = apply_pending(brain_root)

    facts = _rows(brain_root, "SELECT * FROM facts")
    assert report.applied == 1
    assert report.skipped == 1
    assert len(report.errors) == 1
    assert "2026-04-28_001_low_confidence_fact.md" in report.errors[0]
    assert facts[0]["object"] == "artist"
    assert _archived_review(brain_root, "2026-04-28_002_low_confidence_fact.md").exists()


@requires_review_pipeline
def test_apply_new_entity_then_pending_fact_adds_fact_and_page(brain_root: Path) -> None:
    _write_review(
        brain_root,
        "2026-04-28_001_new_entity_review",
        "new_entity_review",
        "\n".join(
            [
                "# New entity needs review",
                "",
                "- name: 小张",
                "- type: person",
                "- confidence: 0.95",
                "",
                "## Resolution",
                "",
                "- slug: zhang-san",
                "- merge_into:",
            ]
        ),
        checked="approve",
    )
    _write_review(
        brain_root,
        "2026-04-28_002_pending_fact",
        "pending_fact",
        _pending_fact_body(
            FactCandidate(
                subject="小张",
                predicate="works_on",
                object="recommendations",
                object_type=FactObjectType.LITERAL,
                valid_from="2026-04-28",
                source_event=SECOND_ULID,
                source_ref="laundry/xiao-zhang.md",
                confidence=0.9,
            )
        ),
        checked="approve",
    )

    report = apply_pending(brain_root)

    facts = _rows(brain_root, "SELECT * FROM facts")
    aliases = _rows(brain_root, "SELECT * FROM entity_aliases")
    page = parse_page(brain_root / "pages" / "entities" / "zhang-san.md")
    assert report.applied == 2
    assert facts[0]["subject"] == "zhang-san"
    assert facts[0]["predicate"] == "works_on"
    assert aliases[0]["alias"] == "小张"
    assert aliases[0]["entity_id"] == "zhang-san"
    assert page.frontmatter.slug == "zhang-san"
    assert page.frontmatter.type is PageType.ENTITY
    assert _archived_review(brain_root, "2026-04-28_001_new_entity_review.md").exists()
    assert _archived_review(brain_root, "2026-04-28_002_pending_fact.md").exists()


@requires_review_pipeline
def test_pending_fact_follow_up_review_is_not_error(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_entity_and_page(brain_root)
    _write_review(
        brain_root,
        "2026-04-28_001_pending_fact",
        "pending_fact",
        _pending_fact_body(_candidate()),
        checked="approve",
    )

    def fake_handle_candidate(**kwargs: Any) -> None:
        report = kwargs["report"]
        report.review_files.append("review/2026-04-28_002_fact_conflict.md")

    ingest_pipeline = importlib.import_module("brain.pipeline.ingest")
    monkeypatch.setattr(ingest_pipeline, "_handle_candidate", fake_handle_candidate)
    monkeypatch.setattr(ingest_pipeline, "_rebuild_touched_backlinks", lambda *args: None)

    report = apply_pending(brain_root)

    assert report.applied == 1
    assert report.skipped == 0
    assert report.errors == []
    assert report.follow_ups == [
        "Pending fact produced review item: review/2026-04-28_002_fact_conflict.md"
    ]
    assert report.reports[0].follow_ups == report.follow_ups


@requires_review_pipeline
def test_apply_tier_proposal_updates_entity_and_rewrites_page(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_entity_and_page(brain_root)
    review_file = _write_review(
        brain_root,
        "2026-04-28_001_tier_proposal",
        "tier_proposal",
        _tier_body(),
        checked="approve",
    )
    with connect(brain_root / "brain.db") as conn:
        propose_tier(
            conn,
            entity_id="alice",
            target_tier=Tier.TIER_2,
            reason="mention_count 3 reached tier 2 threshold",
            review_file=review_file.relative_to(brain_root).as_posix(),
        )
        conn.commit()

    calls: list[tuple[int, str | None]] = []

    def fake_rewrite(timeline: list[Any], current_truth: str | None) -> str:
        calls.append((len(timeline), current_truth))
        return "rewritten truth"

    monkeypatch.setattr(review_pipeline.llm_client, "rewrite_compiled_truth", fake_rewrite)

    report = apply_pending(brain_root)

    entity = _rows(brain_root, "SELECT tier FROM entities WHERE id = ?", ("alice",))[0]
    proposal = _rows(brain_root, "SELECT decision FROM tier_proposals")[0]
    page = parse_page(brain_root / "pages" / "entities" / "alice.md")
    assert report.applied == 1
    assert entity["tier"] == 2
    assert proposal["decision"] == "approved"
    assert calls == [(1, "old truth")]
    assert page.compiled_truth == "rewritten truth"
    assert page.frontmatter.tier is Tier.TIER_2


@requires_review_pipeline
def test_reject_archives_without_fact_write_and_defer_keeps_pending(brain_root: Path) -> None:
    _write_review(
        brain_root,
        "2026-04-28_001_low_confidence_fact",
        "low_confidence_fact",
        _low_confidence_body(_candidate()),
        checked="reject",
    )
    _write_review(
        brain_root,
        "2026-04-28_002_low_confidence_fact",
        "low_confidence_fact",
        _low_confidence_body(_candidate(object_value="artist")),
        checked="defer",
    )

    report = apply_pending(brain_root)

    assert report.applied == 1
    assert report.skipped == 1
    assert _scalar(brain_root, "SELECT COUNT(*) FROM facts") == 0
    assert (brain_root / "review" / "archive" / "2026-04-28_001_low_confidence_fact.md").exists()
    assert (brain_root / "review" / "2026-04-28_002_low_confidence_fact.md").exists()


@requires_review_pipeline
def test_unsupported_new_entity_approve_reports_error(brain_root: Path) -> None:
    _write_review(
        brain_root,
        "2026-04-28_001_new_entity_review",
        "new_entity_review",
        "# New entity needs review\n\n- name: 张三",
        checked="approve",
    )

    report = apply_pending(brain_root)

    assert report.applied == 0
    assert len(report.errors) == 1
    assert "requires slug or merge_into" in report.errors[0]


@requires_review_pipeline
def test_review_source_ref_external_path_does_not_leak_absolute_path(
    brain_root: Path,
    tmp_path: Path,
) -> None:
    external = tmp_path / "outside" / "review" / "2026-04-28_001_low_confidence_fact.md"
    external.parent.mkdir(parents=True)
    external.write_text("", encoding="utf-8")

    source_ref = review_pipeline._review_source_ref(BrainPaths(brain_root), external)

    assert source_ref == "external_review:2026-04-28_001_low_confidence_fact.md"
    assert str(external.parent) not in source_ref
    assert not Path(source_ref).is_absolute()


def test_cli_review_lists_pending_items_and_filters_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, str | None]] = []
    all_items = [
        SimpleNamespace(
            review_id="2026-04-28_001_low_confidence_fact",
            kind="low_confidence_fact",
            status="pending",
            path=tmp_path / "review" / "2026-04-28_001_low_confidence_fact.md",
        ),
        SimpleNamespace(
            review_id="2026-04-28_002_fact_conflict",
            kind="fact_conflict",
            status="pending",
            path=tmp_path / "review" / "2026-04-28_002_fact_conflict.md",
        ),
    ]

    def fake_list_pending(root: Path, *, kind: str | None = None) -> list[Any]:
        calls.append((root, kind))
        if kind is None:
            return all_items
        return [item for item in all_items if item.kind == kind]

    monkeypatch.setattr(review_cli, "list_pending", fake_list_pending)
    monkeypatch.chdir(tmp_path)

    all_result = runner.invoke(app, ["review"])
    filtered_result = runner.invoke(app, ["review", "--kind", "fact_conflict"])

    assert all_result.exit_code == 0
    assert "Pending review items:" in all_result.stdout
    assert "2026-04-28_001_low_confidence_fact" in all_result.stdout
    assert "2026-04-28_002_fact_conflict" in all_result.stdout
    assert filtered_result.exit_code == 0
    assert "2026-04-28_001_low_confidence_fact" not in filtered_result.stdout
    assert "2026-04-28_002_fact_conflict" in filtered_result.stdout
    assert calls == [(tmp_path, None), (tmp_path, "fact_conflict")]


def test_cli_review_apply_scans_pending_and_passes_kind_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, str | None]] = []

    def fake_apply_pending(root: Path, *, kind: str | None = None) -> SimpleNamespace:
        calls.append((root, kind))
        return SimpleNamespace(
            applied=2,
            approved=1,
            rejected=1,
            deferred=0,
            skipped=3,
            follow_ups=["review/2026-04-28_003_fact_conflict.md"],
            errors=[],
        )

    monkeypatch.setattr(review_cli, "apply_pending", fake_apply_pending)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["review", "--apply", "--kind", "fact_conflict"])

    assert result.exit_code == 0
    assert calls == [(tmp_path, "fact_conflict")]
    assert "Review apply summary:" in result.stdout
    assert "applied=2" in result.stdout
    assert "approved=1" in result.stdout
    assert "rejected=1" in result.stdout
    assert "deferred=0" in result.stdout
    assert "skipped=3" in result.stdout
    assert "follow_ups=1" in result.stdout
    assert "review/2026-04-28_003_fact_conflict.md" in result.stdout


@requires_review_pipeline
def test_cli_review_lists_applies_and_opens_by_prefix(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_review(
        brain_root,
        "2026-04-28_001_low_confidence_fact",
        "low_confidence_fact",
        _low_confidence_body(_candidate()),
        checked="approve",
    )
    monkeypatch.chdir(brain_root)

    list_result = runner.invoke(app, ["review"])
    filter_result = runner.invoke(app, ["review", "--kind", "low_confidence_fact"])
    apply_result = runner.invoke(app, ["review", "--apply"])

    assert list_result.exit_code == 0
    assert "2026-04-28_001_low_confidence_fact" in list_result.stdout
    assert filter_result.exit_code == 0
    assert "low_confidence_fact" in filter_result.stdout
    assert apply_result.exit_code == 0
    assert "Review apply summary: applied=1" in apply_result.stdout

    _write_review(
        brain_root,
        "2026-04-28_002_low_confidence_fact",
        "low_confidence_fact",
        _low_confidence_body(_candidate(object_value="artist")),
    )
    opened: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        opened.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("EDITOR", "test-editor")
    monkeypatch.setattr("brain.cli.review.subprocess.run", fake_run)

    open_result = runner.invoke(app, ["review", "2026-04-28_002"])

    assert open_result.exit_code == 0
    assert opened == [["test-editor", str(brain_root / "review" / "2026-04-28_002_low_confidence_fact.md")]]


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("approve", "decision=approve"),
        ("reject", "decision=reject"),
        ("defer", "decision=defer"),
        ("none", "no decision selected"),
    ],
)
def test_cli_review_open_reports_parsed_decision(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected: str,
) -> None:
    review_path = brain_root / "review" / "2026-04-28_001_low_confidence_fact.md"
    opened: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        opened.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.chdir(brain_root)
    monkeypatch.setenv("EDITOR", "test-editor")
    monkeypatch.setattr("brain.cli.review.resolve_review_path", lambda root, review_id: review_path)
    monkeypatch.setattr("brain.cli.review.parse_review_file", lambda path: SimpleNamespace(action=action))
    monkeypatch.setattr("brain.cli.review.subprocess.run", fake_run)

    result = runner.invoke(app, ["review", "2026-04-28_001"])

    assert result.exit_code == 0
    assert opened == [["test-editor", str(review_path)]]
    assert expected in result.stdout


def _write_review(
    root: Path,
    review_id: str,
    kind: str,
    body: str,
    *,
    checked: str | None = None,
) -> Path:
    path = root / "review" / f"{review_id}.md"
    decision_lines = []
    for action in ("approve", "reject", "defer"):
        mark = "x" if checked == action else " "
        decision_lines.append(f"[{mark}] {action}")
    text = "\n".join(
        [
            "---",
            f"review_id: {review_id}",
            f"kind: {kind}",
            "created: 2026-04-28T12:00:00+00:00",
            "status: pending",
            "---",
            "",
            body.strip(),
            "",
            "## Decision",
            "",
            *decision_lines,
            "",
        ]
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _low_confidence_body(candidate: FactCandidate) -> str:
    return "\n".join(
        [
            "# Low confidence fact",
            "",
            "```json",
            json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False, indent=2),
            "```",
        ]
    )


def _fact_conflict_body(candidate: FactCandidate, facts: list[Fact]) -> str:
    return "\n".join(
        [
            "# Fact conflict",
            "",
            "## Candidate",
            "",
            "```json",
            json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Active facts",
            "",
            "```json",
            json.dumps([fact.model_dump(mode="json") for fact in facts], ensure_ascii=False, indent=2),
            "```",
        ]
    )


def _pending_fact_body(candidate: FactCandidate) -> str:
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "event": {
            "id": candidate.source_event,
            "timestamp": "2026-04-28T12:00:00Z",
            "kind": "laundry_ingested",
            "source_ref": candidate.source_ref,
            "raw_payload": "小张 will work on recommendations.",
            "raw_payload_path": None,
            "extracted_facts": [],
            "affected_pages": [],
            "confidence": 1.0,
            "metadata": {},
        },
        "timeline_summary": "小张 will work on recommendations.",
        "suggested_page_type": "entity",
        "unresolved_entities": ["小张"],
    }
    return "\n".join(
        [
            "# Pending fact",
            "",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "```",
        ]
    )


def _tier_body() -> str:
    return "\n".join(
        [
            "# Tier proposal",
            "",
            "- entity_id: alice",
            "- current_tier: 3",
            "- proposed_tier: 2",
            "- mention_count: 3",
            "- reason: mention_count 3 reached tier 2 threshold",
        ]
    )


def _candidate(
    *,
    object_value: str = "engineer",
    valid_from: str = "2026-04-01",
) -> FactCandidate:
    return FactCandidate(
        subject="alice",
        predicate="role",
        object=object_value,
        object_type=FactObjectType.LITERAL,
        valid_from=valid_from,
        valid_to=None,
        source_event=SECOND_ULID,
        source_ref="review/test.md",
        confidence=0.7,
    )


def _insert_fact(brain_root: Path, *, object_value: str) -> Fact:
    fact = Fact(
        subject="alice",
        predicate="role",
        object=object_value,
        object_type=FactObjectType.LITERAL,
        valid_from="2026-04-01",
        valid_to=None,
        asserted_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
        source_event=VALID_ULID,
        source_ref="events.jsonl",
        confidence=0.9,
    )
    with connect(brain_root / "brain.db") as conn:
        fact_id = add_fact(conn, fact)
        conn.commit()
    return fact.model_copy(update={"id": fact_id})


def _insert_entity_and_page(brain_root: Path) -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    entity = Entity(
        id="alice",
        type=EntityType.PERSON,
        title="Alice",
        page_path="pages/entities/alice.md",
        tier=Tier.TIER_3,
        mention_count=3,
        first_seen=now,
        last_seen=now,
        metadata={},
    )
    with connect(brain_root / "brain.db") as conn:
        upsert_entity(conn, entity)
        conn.commit()
    write_page(
        brain_root / "pages" / "entities" / "alice.md",
        Page(
            frontmatter=Frontmatter(
                type=PageType.ENTITY,
                slug="alice",
                title="Alice",
                tier=Tier.TIER_3,
                created=now,
                updated=now,
                tags=[],
                aliases=[],
                external_ids={},
            ),
            compiled_truth="old truth",
            timeline=[f"- 2026-04-28 [event:{VALID_ULID}]: Alice did work"],
            sources=[],
        ),
    )


def _archived_review(root: Path, name: str = "2026-04-28_001_fact_conflict.md") -> Path:
    return root / "review" / "archive" / name


def _rows(brain_root: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(brain_root / "brain.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _scalar(brain_root: Path, sql: str, params: tuple[object, ...] = ()) -> Any:
    return _rows(brain_root, sql, params)[0][0]


def _git_log_messages(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "log", "--format=%s"],
        capture_output=True,
        encoding="utf-8",
        env=_git_env(),
        check=True,
    )
    return result.stdout.splitlines()


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env
