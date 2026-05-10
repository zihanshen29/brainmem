from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli.main import app
from brain.exceptions import BrainError
from brain.models import Frontmatter, Page, PageType, ProcedureStatus
from brain.models.event import ULID_PATTERN
from brain.pages import parse_entry, parse_page, write_page
from brain.pipeline.procedure import create_procedure, promote_procedure, run_procedure

runner = CliRunner()


def test_procedure_new_creates_page_and_index(brain_root: Path) -> None:
    report = create_procedure(
        brain_root,
        "daily-review",
        title="Daily Review",
        auto_commit=False,
    )

    assert report.path == "pages/procedures/daily-review.md"
    page = parse_page(brain_root / report.path)
    assert page.frontmatter.type is PageType.PROCEDURE
    assert page.frontmatter.status is ProcedureStatus.RAW
    assert page.frontmatter.success_count == 0
    assert page.frontmatter.fail_count == 0
    assert page.frontmatter.last_run is None
    assert "Daily Review" in page.compiled_truth
    assert page.timeline == []
    assert "- [Daily Review](procedures/daily-review.md)" in (
        brain_root / "pages" / "index.md"
    ).read_text(encoding="utf-8")


def test_procedure_new_rejects_duplicate_slug_and_invalid_slug(brain_root: Path) -> None:
    create_procedure(brain_root, "daily-review", title="Daily Review", auto_commit=False)

    with pytest.raises(BrainError, match="already exists"):
        create_procedure(brain_root, "daily-review", title="Duplicate", auto_commit=False)

    with pytest.raises(BrainError, match="slug must be lowercase"):
        create_procedure(brain_root, "Daily_Review", title="Invalid", auto_commit=False)


def test_procedure_run_success_and_fail_update_counts_status_last_run_and_timeline(
    brain_root: Path,
) -> None:
    create_procedure(brain_root, "daily-review", title="Daily Review", auto_commit=False)

    success = run_procedure(
        brain_root,
        "daily-review",
        result="success",
        note="worked cleanly",
        auto_commit=False,
    )
    failed = run_procedure(
        brain_root,
        "daily-review",
        result="fail",
        note="missing source file",
        auto_commit=False,
    )

    page = parse_page(brain_root / "pages" / "procedures" / "daily-review.md")
    assert success.status is ProcedureStatus.TESTED
    assert failed.status is ProcedureStatus.TESTED
    assert page.frontmatter.status is ProcedureStatus.TESTED
    assert page.frontmatter.success_count == 1
    assert page.frontmatter.fail_count == 1
    assert page.frontmatter.last_run is not None
    assert page.frontmatter.last_run.tzinfo is not None
    assert page.frontmatter.last_run.astimezone(UTC) == page.frontmatter.last_run
    assert len(page.timeline) == 2
    assert "Procedure run success: worked cleanly" in page.timeline[0]
    assert "Procedure run fail: missing source file" in page.timeline[1]
    assert ULID_PATTERN.fullmatch(parse_entry(page.timeline[0]).event_id)
    assert ULID_PATTERN.fullmatch(parse_entry(page.timeline[1]).event_id)
    assert _ledger_events(brain_root)[-2]["id"] == success.event_id
    assert _ledger_events(brain_root)[-1]["id"] == failed.event_id


def test_procedure_auto_promotes_to_stable_after_configured_successes(
    brain_root: Path,
) -> None:
    config_path = brain_root / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "stable_success_threshold = 5",
            "stable_success_threshold = 2",
        ),
        encoding="utf-8",
        newline="\n",
    )
    create_procedure(brain_root, "daily-review", title="Daily Review", auto_commit=False)

    first = run_procedure(
        brain_root,
        "daily-review",
        result="success",
        note="first clean run",
        auto_commit=False,
    )
    second = run_procedure(
        brain_root,
        "daily-review",
        result="success",
        note="second clean run",
        auto_commit=False,
    )

    page = parse_page(brain_root / "pages" / "procedures" / "daily-review.md")
    assert first.status is ProcedureStatus.TESTED
    assert second.status is ProcedureStatus.STABLE
    assert page.frontmatter.status is ProcedureStatus.STABLE
    assert page.frontmatter.success_count == 2
    assert page.frontmatter.fail_count == 0


def test_procedure_stable_downgrades_after_configured_failures(brain_root: Path) -> None:
    create_procedure(brain_root, "daily-review", title="Daily Review", auto_commit=False)
    promote_procedure(brain_root, "daily-review", status="stable", auto_commit=False)

    first = run_procedure(
        brain_root,
        "daily-review",
        result="fail",
        note="temporary issue",
        auto_commit=False,
    )
    second = run_procedure(
        brain_root,
        "daily-review",
        result="fail",
        note="repeat issue",
        auto_commit=False,
    )

    page = parse_page(brain_root / "pages" / "procedures" / "daily-review.md")
    assert first.status is ProcedureStatus.STABLE
    assert second.status is ProcedureStatus.TESTED
    assert page.frontmatter.status is ProcedureStatus.TESTED
    assert page.frontmatter.fail_count == 2


def test_procedure_tested_downgrades_to_raw_when_failures_overtake_successes(
    brain_root: Path,
) -> None:
    create_procedure(brain_root, "daily-review", title="Daily Review", auto_commit=False)
    run_procedure(
        brain_root,
        "daily-review",
        result="success",
        note="clean run",
        auto_commit=False,
    )
    first_fail = run_procedure(
        brain_root,
        "daily-review",
        result="fail",
        note="first issue",
        auto_commit=False,
    )
    second_fail = run_procedure(
        brain_root,
        "daily-review",
        result="fail",
        note="second issue",
        auto_commit=False,
    )

    page = parse_page(brain_root / "pages" / "procedures" / "daily-review.md")
    assert first_fail.status is ProcedureStatus.TESTED
    assert second_fail.status is ProcedureStatus.RAW
    assert page.frontmatter.status is ProcedureStatus.RAW
    assert page.frontmatter.success_count == 1
    assert page.frontmatter.fail_count == 2


def test_procedure_promote_updates_status_and_appends_timeline(brain_root: Path) -> None:
    create_procedure(brain_root, "daily-review", title="Daily Review", auto_commit=False)

    report = promote_procedure(
        brain_root,
        "daily-review",
        status="stable",
        auto_commit=False,
    )

    page = parse_page(brain_root / "pages" / "procedures" / "daily-review.md")
    assert report.status is ProcedureStatus.STABLE
    assert page.frontmatter.status is ProcedureStatus.STABLE
    assert page.frontmatter.success_count == 0
    assert page.frontmatter.fail_count == 0
    assert page.timeline == [
        f"- {parse_entry(page.timeline[0]).date} [event:{report.event_id}]: "
        "Procedure status set to stable."
    ]
    assert ULID_PATTERN.fullmatch(parse_entry(page.timeline[0]).event_id)


def test_procedure_unknown_slug_and_non_procedure_page_are_rejected(brain_root: Path) -> None:
    with pytest.raises(BrainError, match="Unknown procedure"):
        run_procedure(
            brain_root,
            "missing",
            result="success",
            note="no page",
            auto_commit=False,
        )

    path = brain_root / "pages" / "procedures" / "not-procedure.md"
    write_page(
        path,
        Page(
            frontmatter=Frontmatter(
                type=PageType.PROJECT,
                slug="not-procedure",
                title="Not Procedure",
                created="2026-05-01T00:00:00Z",
                updated="2026-05-01T00:00:00Z",
            ),
            compiled_truth="Project page in the procedure directory.",
            timeline=[],
            sources=[],
        ),
    )

    with pytest.raises(BrainError, match="not a procedure"):
        promote_procedure(brain_root, "not-procedure", status="tested", auto_commit=False)


def test_cli_procedure_summaries_are_clear(brain_root: Path) -> None:
    created = runner.invoke(
        app,
        [
            "procedure",
            "new",
            "daily-review",
            "--title",
            "Daily Review",
            "--brain-root",
            str(brain_root),
        ],
    )
    assert created.exit_code == 0
    assert "Procedure new summary:" in created.stdout
    assert "slug=daily-review" in created.stdout
    assert "path=pages/procedures/daily-review.md" in created.stdout
    assert "status=raw" in created.stdout

    ran = runner.invoke(
        app,
        [
            "procedure",
            "run",
            "daily-review",
            "--result",
            "success",
            "--note",
            "ok",
            "--brain-root",
            str(brain_root),
        ],
    )
    assert ran.exit_code == 0
    assert "Procedure run summary:" in ran.stdout
    assert "status=tested" in ran.stdout
    assert "success_count=1" in ran.stdout
    assert "event_id=" in ran.stdout

    promoted = runner.invoke(
        app,
        [
            "procedure",
            "promote",
            "daily-review",
            "--status",
            "stable",
            "--brain-root",
            str(brain_root),
        ],
    )
    assert promoted.exit_code == 0
    assert "Procedure promote summary:" in promoted.stdout
    assert "status=stable" in promoted.stdout


def test_cli_procedure_errors_are_clear(brain_root: Path) -> None:
    result = runner.invoke(
        app,
        [
            "procedure",
            "run",
            "missing",
            "--result",
            "success",
            "--note",
            "no page",
            "--brain-root",
            str(brain_root),
        ],
    )

    assert result.exit_code == 1
    assert "Error: Unknown procedure: missing" in result.stderr


def _ledger_events(brain_root: Path) -> list[dict[str, object]]:
    lines = (brain_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]
