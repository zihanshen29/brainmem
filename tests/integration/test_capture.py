from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import frontmatter
import pytest
from typer.testing import CliRunner

from brain.cli.init import init_brain
from brain.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


@pytest.fixture()
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    return root


def test_cli_capture_stdin_writes_laundry_frontmatter_body_path_and_output(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture", "--stdin"], input="Hello capture\r\nBody line\n")

    assert result.exit_code == 0
    report_path = _output_value(result.stdout, "path")
    assert report_path.startswith("laundry/")
    assert report_path.endswith("_hello-capture.md")
    assert "kind=note" in result.stdout
    assert "committed=true" in result.stdout

    post = frontmatter.loads((brain_root / report_path).read_text(encoding="utf-8"))
    assert post.metadata["kind"] == "note"
    assert post.metadata["source"] == "stdin"
    assert "captured" in post.metadata
    assert post.content == "Hello capture\nBody line"


def test_cli_capture_file_reads_input_and_leaves_original_unchanged(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "capture-source.md"
    original = "File capture\nBody\n"
    source.write_text(original, encoding="utf-8", newline="\n")
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture", "idea", "--file", str(source)])

    assert result.exit_code == 0
    assert source.read_text(encoding="utf-8") == original
    post = _single_capture_post(brain_root)
    assert post.metadata["kind"] == "idea"
    assert post.metadata["source"] == "file"
    assert post.metadata["source_ref"] == str(source)
    assert post.content == "File capture\nBody"


def test_cli_capture_editor_reads_tempfile_written_by_editor(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(args[-1]).write_text("Editor capture\n", encoding="utf-8", newline="\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr("brain.cli.capture.subprocess.run", fake_run)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture", "meeting"])

    assert result.exit_code == 0
    post = _single_capture_post(brain_root)
    assert post.metadata["kind"] == "meeting"
    assert post.metadata["source"] == "editor"
    assert "source_ref" not in post.metadata
    assert post.content == "Editor capture"


def test_cli_capture_editor_splits_editor_with_arguments(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_args: list[str] = []

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_args.extend(args)
        Path(args[-1]).write_text("Editor args capture\n", encoding="utf-8", newline="\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setenv("EDITOR", "code --wait")
    monkeypatch.setattr("brain.cli.capture.subprocess.run", fake_run)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture"])

    assert result.exit_code == 0
    assert captured_args[:2] == ["code", "--wait"]
    assert captured_args[-1].endswith(".md")
    assert _single_capture_post(brain_root).content == "Editor args capture"


@pytest.mark.parametrize("kind", ["idea", "meeting", "chat"])
def test_cli_capture_supported_kinds_write_frontmatter(
    brain_root: Path,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture", kind, "--stdin"], input=f"{kind} content\n")

    assert result.exit_code == 0
    post = _single_capture_post(brain_root)
    assert post.metadata["kind"] == kind


def test_cli_capture_empty_input_reports_error(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture", "--stdin"], input=" \n\t")

    assert result.exit_code == 1
    assert "Error: Capture content is empty" in result.stderr
    assert _capture_files(brain_root) == []


def test_cli_capture_source_mutual_exclusion_reports_error(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("content\n", encoding="utf-8")
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture", "--stdin", "--file", str(source)], input="content\n")

    assert result.exit_code == 1
    assert "Error: Choose only one capture source" in result.stderr
    assert _capture_files(brain_root) == []


def test_cli_capture_missing_file_reports_error(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture", "--file", str(tmp_path / "missing.md")])

    assert result.exit_code == 1
    assert "Error: Capture file not found:" in result.stderr
    assert _capture_files(brain_root) == []


def test_cli_capture_editor_failure_reports_error(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 2)

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr("brain.cli.capture.subprocess.run", fake_run)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture"])

    assert result.exit_code == 1
    assert "Error: Editor failed with exit code 2" in result.stderr
    assert _capture_files(brain_root) == []


def test_cli_capture_does_not_trigger_ingest_events_or_processed(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_before = (brain_root / "events.jsonl").read_text(encoding="utf-8")
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["capture", "--stdin"], input="Do not ingest me\n")

    assert result.exit_code == 0
    assert (brain_root / "events.jsonl").read_text(encoding="utf-8") == events_before
    assert _capture_files(brain_root)
    assert list((brain_root / "laundry" / "processed").glob("*.md")) == []


def _single_capture_post(brain_root: Path) -> frontmatter.Post:
    captures = _capture_files(brain_root)
    assert len(captures) == 1
    return frontmatter.loads(captures[0].read_text(encoding="utf-8"))


def _capture_files(brain_root: Path) -> list[Path]:
    return sorted(
        path
        for path in (brain_root / "laundry").glob("*.md")
        if path.parent.name != "processed"
    )


def _output_value(output: str, key: str) -> str:
    prefix = f"{key}="
    for part in output.split():
        if part.startswith(prefix):
            return part.removeprefix(prefix)
    raise AssertionError(f"Missing {key}= in output: {output}")
