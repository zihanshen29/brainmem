from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli.main import app

runner = CliRunner()


def test_cli_scratch_append_stdin_writes_working_from_non_root_cwd(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["scratch", "append", "--brain-root", str(brain_root), "--stdin", "--source", "codex"],
        input="scratch note\n",
    )

    assert result.exit_code == 0
    assert "Scratch append summary:" in result.stdout
    assert "source=codex" in result.stdout
    working = brain_root / "scratch" / "working.md"
    content = working.read_text(encoding="utf-8")
    assert "source: codex" in content
    assert "scratch note" in content


def test_cli_scratch_append_argument_writes_short_text(brain_root: Path) -> None:
    result = runner.invoke(
        app,
        ["scratch", "append", "--brain-root", str(brain_root), "--source", "manual", "argument text"],
    )

    assert result.exit_code == 0
    content = (brain_root / "scratch" / "working.md").read_text(encoding="utf-8")
    assert "source: manual" in content
    assert "argument text" in content


def test_cli_snapshot_rebuild_generates_snapshot_from_working(brain_root: Path) -> None:
    append_result = runner.invoke(
        app,
        ["scratch", "append", "--brain-root", str(brain_root), "--stdin", "--source", "codex"],
        input="first\nsecond\n",
    )
    assert append_result.exit_code == 0

    result = runner.invoke(
        app,
        ["snapshot", "rebuild", "--brain-root", str(brain_root), "--max-items", "2", "--max-chars", "80"],
    )

    assert result.exit_code == 0
    assert "Snapshot rebuild summary:" in result.stdout
    assert "path=scratch/SNAPSHOT.md" in result.stdout
    assert "items=1" in result.stdout
    assert "chars=12" in result.stdout
    snapshot = (brain_root / "scratch" / "SNAPSHOT.md").read_text(encoding="utf-8")
    assert "Entries: 1" in snapshot
    assert "source: codex" in snapshot
    assert "first\nsecond" in snapshot


def test_cli_scratch_append_empty_stdin_reports_error(brain_root: Path) -> None:
    result = runner.invoke(
        app,
        ["scratch", "append", "--brain-root", str(brain_root), "--stdin"],
        input=" \n\t",
    )

    assert result.exit_code == 1
    assert "Error: Scratch input is empty" in result.stderr
    assert not (brain_root / "scratch" / "working.md").exists()


def test_cli_snapshot_rebuild_missing_working_reports_error(brain_root: Path) -> None:
    result = runner.invoke(app, ["snapshot", "rebuild", "--brain-root", str(brain_root)])

    assert result.exit_code == 1
    assert "Error: Working scratch buffer not found:" in result.stderr
    assert not (brain_root / "scratch" / "SNAPSHOT.md").exists()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["scratch", "--help"], "Local-only deterministic scratch commands"),
        (["snapshot", "--help"], "Local-only deterministic snapshot commands"),
    ],
)
def test_cli_scratch_snapshot_help_declares_local_only_deterministic(
    args: list[str],
    expected: str,
) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 0
    assert expected in result.stdout
    assert "No external provider is called" in result.stdout
