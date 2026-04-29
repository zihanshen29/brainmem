import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli.init import init_brain
from brain.cli.main import app
from brain.config import load_config
from brain.exceptions import BrainError

runner = CliRunner()


def test_init_creates_files_dirs_db_and_initial_commit(tmp_path: Path) -> None:
    root = tmp_path / "brain"

    init_brain(root)

    expected_dirs = [
        root / "raw",
        root / "laundry" / "processed",
        root / "pages",
        root / "pages" / "entities",
        root / "pages" / "projects",
        root / "pages" / "concepts",
        root / "pages" / "events",
        root / "pages" / "experiences",
        root / "pages" / "conversations",
        root / "review" / "archive",
    ]
    expected_files = [
        root / "config.toml",
        root / "brain.db",
        root / "events.jsonl",
        root / "pages" / "index.md",
        root / "pages" / "log.md",
        root / "README.md",
        root / ".gitignore",
        root / ".gitattributes",
        root / "CLAUDE.md",
    ]

    for directory in expected_dirs:
        assert directory.is_dir()
    for path in expected_files:
        assert path.is_file()

    assert (root / "events.jsonl").read_text(encoding="utf-8") == ""
    assert "*.jsonl text eol=lf" in (root / ".gitattributes").read_text(encoding="utf-8")
    config = load_config(root / "config.toml")
    assert config.paths.brain_root == root.resolve()
    assert config.openai is not None
    assert config.openai.api_key_env == "OPENAI_API_KEY"
    assert config.openai.model == "gpt-5.5"
    assert config.openai.fast_model == "gpt-5.4-mini"
    assert config.anthropic is None
    assert _db_user_version(root / "brain.db") == 1
    assert _git_log_messages(root) == ["Initialize brain repository"]


def test_duplicate_init_raises(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    init_brain(root)

    with pytest.raises(BrainError):
        init_brain(root)


def test_force_init_clears_and_rebuilds_existing_root(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    init_brain(root)
    stale_file = root / "stale.txt"
    stale_file.write_text("remove me", encoding="utf-8")

    init_brain(root, force=True)

    assert not stale_file.exists()
    assert (root / "config.toml").is_file()
    assert (root / "brain.db").is_file()
    assert _db_user_version(root / "brain.db") == 1
    assert _git_log_messages(root) == ["Initialize brain repository"]


def test_init_cli_preserves_version_and_initializes_root(tmp_path: Path) -> None:
    root = tmp_path / "brain"

    version_result = runner.invoke(app, ["--version"])
    command_result = runner.invoke(app, ["init", "--root", str(root)])

    assert version_result.exit_code == 0
    assert version_result.stdout.strip() == "0.1.0"
    assert command_result.exit_code == 0
    assert (root / "config.toml").is_file()
    assert _git_log_messages(root) == ["Initialize brain repository"]


def _db_user_version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


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
