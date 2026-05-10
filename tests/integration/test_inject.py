from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli.init import init_brain
from brain.cli.main import app
from brain.models import Frontmatter, Page, PageType, Tier
from brain.pages import write_page
from brain.pipeline.injection import estimate_tokens

runner = CliRunner()


@pytest.fixture()
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    _seed_pages(root)
    return root


def test_cli_inject_outputs_markdown_context(brain_root: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inject",
            "--query",
            "Alice injection",
            "--budget",
            "300",
            "--format",
            "markdown",
            "--mode",
            "keyword-only",
            "--top",
            "2",
            "--brain-root",
            str(brain_root),
        ],
    )

    assert result.exit_code == 0
    assert "# BrainMem Injection" in result.stdout
    assert "Budget used:" in result.stdout
    assert "Fragment count: 2" in result.stdout
    assert "alice (pages/entities/alice.md)" in result.stdout
    assert "### Alice (`alice`)" in result.stdout
    assert estimate_tokens(result.stdout) <= 300
    assert result.stderr == ""


def test_cli_inject_text_output(brain_root: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inject",
            "--query",
            "Alice injection",
            "--budget",
            "300",
            "--format",
            "text",
            "--mode",
            "keyword-only",
            "--top",
            "1",
            "--brain-root",
            str(brain_root),
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.startswith("BrainMem Injection\n")
    assert "# BrainMem Injection" not in result.stdout
    assert "Alice [alice]" in result.stdout
    assert "Source: pages/entities/alice.md" in result.stdout
    assert estimate_tokens(result.stdout) <= 300


def test_cli_inject_small_budget_reports_truncated_and_skipped(brain_root: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inject",
            "--query",
            "Alice injection",
            "--budget",
            "42",
            "--format",
            "markdown",
            "--mode",
            "keyword-only",
            "--top",
            "2",
            "--brain-root",
            str(brain_root),
        ],
    )

    assert result.exit_code == 0
    assert estimate_tokens(result.stdout) <= 42
    assert "Budget used:" in result.stdout
    assert "Truncated: yes" in result.stdout
    assert "Skipped: brain-injection" in result.stdout
    assert "Warning: One or more fragments were truncated" in result.stderr
    assert "Warning: One or more fragments were skipped" in result.stderr


def test_cli_inject_includes_snapshot_by_default(brain_root: Path) -> None:
    _write_snapshot(brain_root, "Snapshot context is included before retrieved pages.")

    result = runner.invoke(
        app,
        [
            "inject",
            "--query",
            "Alice injection",
            "--budget",
            "500",
            "--brain-root",
            str(brain_root),
        ],
    )

    assert result.exit_code == 0
    assert estimate_tokens(result.stdout) <= 500
    assert "snapshot (scratch/SNAPSHOT.md)" in result.stdout
    assert result.stdout.index("### Current Snapshot (`snapshot`)") < result.stdout.index(
        "### Alice (`alice`)"
    )


def test_cli_inject_no_snapshot_omits_snapshot(brain_root: Path) -> None:
    _write_snapshot(brain_root, "Snapshot context should be omitted.")

    result = runner.invoke(
        app,
        [
            "inject",
            "--query",
            "Alice injection",
            "--budget",
            "300",
            "--no-snapshot",
            "--brain-root",
            str(brain_root),
        ],
    )

    assert result.exit_code == 0
    assert estimate_tokens(result.stdout) <= 300
    assert "snapshot (scratch/SNAPSHOT.md)" not in result.stdout
    assert "Snapshot context should be omitted." not in result.stdout
    assert "### Alice (`alice`)" in result.stdout


def test_cli_inject_invalid_mode_exits_with_error(brain_root: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inject",
            "--query",
            "Alice",
            "--mode",
            "provider-mode",
            "--brain-root",
            str(brain_root),
        ],
    )

    assert result.exit_code == 1
    assert "Error: unsupported ask mode: provider-mode" in result.stderr


def test_cli_inject_rejects_invalid_budget(brain_root: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inject",
            "--query",
            "Alice",
            "--budget",
            "0",
            "--brain-root",
            str(brain_root),
        ],
    )

    assert result.exit_code != 0
    assert "Invalid value" in result.stderr


def _seed_pages(root: Path) -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    write_page(
        root / "pages" / "entities" / "alice.md",
        Page(
            frontmatter=Frontmatter(
                type=PageType.ENTITY,
                slug="alice",
                title="Alice",
                tier=Tier.TIER_2,
                created=now,
                updated=now,
                tags=[],
                aliases=[],
                external_ids={},
            ),
            compiled_truth=(
                "Alice owns injection operations. "
                "Token-aware memory output should expose source paths."
            ),
            timeline=[
                "- 2026-04-28 [event:01KQA8VZMXBAV7AKF5JFB4KQ9C]: Alice tested injection."
            ],
            sources=["events.jsonl"],
        ),
    )
    write_page(
        root / "pages" / "projects" / "brain-injection.md",
        Page(
            frontmatter=Frontmatter(
                type=PageType.PROJECT,
                slug="brain-injection",
                title="Brain Injection",
                created=now,
                updated=now,
                tags=[],
                aliases=[],
                external_ids={},
            ),
            compiled_truth=(
                "Brain injection retrieves prompt fragments. "
                "It uses keyword-only mode by default for local execution."
            ),
            timeline=[
                "- 2026-04-27 [event:01KQA8R9KVCG906A0203VYEQF7]: Injection CLI was specified."
            ],
            sources=["docs/cli.md"],
        ),
    )


def _write_snapshot(root: Path, content: str) -> None:
    snapshot = root / "scratch" / "SNAPSHOT.md"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(content, encoding="utf-8")
