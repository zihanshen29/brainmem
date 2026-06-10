from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from brain.cli import lint as lint_cli
from brain.cli.init import init_brain
from brain.cli.main import app
from brain.db.connection import connect
from brain.db.facts import add_fact
from brain.exceptions import BrainError
from brain.models import Fact, FactObjectType

runner = CliRunner()
VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"


def test_cli_lint_all_runs_every_kind_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, list[str], int | None]] = []

    def fake_run_lint(root: Path, kinds: list[str], *, stale_days: int | None = None) -> Any:
        calls.append((root, kinds, stale_days))
        return SimpleNamespace(
            issue_counts={
                "contradictions": 2,
                "stale": 1,
                "orphans": 0,
                "citations": 3,
            },
            review_files=[
                "review/2026-04-28_001_lint_contradictions.md",
                "review/2026-04-28_002_lint_citations.md",
            ],
            total_issues=6,
        )

    monkeypatch.setattr(lint_cli, "_run_lint", fake_run_lint)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", "--all"])

    assert result.exit_code == 0
    assert calls == [(tmp_path, ["contradictions", "stale", "orphans", "citations"], None)]
    assert "Lint summary:" in result.stdout
    assert "- contradictions: 2 issues" in result.stdout
    assert "- stale: 1 issues" in result.stdout
    assert "- orphans: 0 issues" in result.stdout
    assert "- citations: 3 issues" in result.stdout
    assert "Review files:" in result.stdout
    assert "review/2026-04-28_001_lint_contradictions.md" in result.stdout
    assert "review/2026-04-28_002_lint_citations.md" in result.stdout
    assert "Total issues: 6" in result.stdout


@pytest.mark.parametrize(
    ("flag", "kind"),
    [
        ("--contradictions", "contradictions"),
        ("--orphans", "orphans"),
        ("--citations", "citations"),
    ],
)
def test_cli_lint_single_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    kind: str,
) -> None:
    calls: list[tuple[Path, list[str], int | None]] = []

    def fake_run_lint(root: Path, kinds: list[str], *, stale_days: int | None = None) -> Any:
        calls.append((root, kinds, stale_days))
        return {"issue_counts": {kind: 1}, "review_files": [], "total_issues": 1}

    monkeypatch.setattr(lint_cli, "_run_lint", fake_run_lint)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", flag])

    assert result.exit_code == 0
    assert calls == [(tmp_path, [kind], None)]
    assert f"- {kind}: 1 issues" in result.stdout
    assert "Total issues: 1" in result.stdout


def test_cli_lint_stale_passes_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, list[str], int | None]] = []

    def fake_run_lint(root: Path, kinds: list[str], *, stale_days: int | None = None) -> Any:
        calls.append((root, kinds, stale_days))
        return SimpleNamespace(issue_counts={"stale": 4}, review_files=["review/stale.md"])

    monkeypatch.setattr(lint_cli, "_run_lint", fake_run_lint)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", "--stale", "--days", "45"])

    assert result.exit_code == 0
    assert calls == [(tmp_path, ["stale"], 45)]
    assert "- stale: 4 issues" in result.stdout
    assert "review/stale.md" in result.stdout
    assert "Total issues: 4" in result.stdout


def test_cli_lint_accepts_brain_root_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    calls: list[tuple[Path, list[str], int | None]] = []

    def fake_run_lint(root_arg: Path, kinds: list[str], *, stale_days: int | None = None) -> Any:
        calls.append((root_arg, kinds, stale_days))
        return {"issue_counts": {"citations": 0}, "review_files": [], "total_issues": 0}

    monkeypatch.setattr(lint_cli, "_run_lint", fake_run_lint)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", "--brain-root", str(root), "--citations"])

    assert result.exit_code == 0
    assert calls == [(root, ["citations"], None)]


def test_cli_lint_brain_error_outputs_stderr_and_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_brain_error(*_args: object, **_kwargs: object) -> None:
        raise BrainError("lint failed")

    monkeypatch.setattr(lint_cli, "_run_lint", raise_brain_error)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["lint", "--citations"])

    assert result.exit_code == 1
    assert "Error: lint failed" in result.stderr


def test_cli_lint_all_writes_review_and_lint_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    init_brain(root)
    _insert_fact(root, object_value="UK", source_event=VALID_ULID)
    _insert_fact(root, object_value="Singapore", source_event=SECOND_ULID)
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["lint", "--all"])

    assert result.exit_code == 0
    assert "- contradictions: 1 issues" in result.stdout
    assert "Total issues: 1" in result.stdout

    review_files = list((root / "review").glob("*.md"))
    assert len(review_files) == 1
    review_text = review_files[0].read_text(encoding="utf-8")
    assert "kind: lint_finding" in review_text
    assert "lint_kind: contradictions" in review_text
    assert "## Decision" in review_text

    rows = _rows(root, "SELECT kind, issue_count, report_file FROM lint_results ORDER BY kind")
    assert rows == [
        ("citations", 0, ""),
        ("contradictions", 1, review_files[0].relative_to(root).as_posix()),
        ("orphans", 0, ""),
        ("stale", 0, ""),
    ]


def _insert_fact(root: Path, *, object_value: str, source_event: str) -> None:
    with connect(root / "brain.db") as conn:
        add_fact(
            conn,
            Fact(
                subject="zihan",
                predicate="location",
                object=object_value,
                object_type=FactObjectType.LITERAL,
                valid_from="2026-04-01",
                valid_to=None,
                asserted_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
                source_event=source_event,
                source_ref="events.jsonl",
                confidence=0.9,
            ),
        )
        conn.commit()


def _rows(root: Path, sql: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(root / "brain.db") as connection:
        return [tuple(row) for row in connection.execute(sql).fetchall()]
