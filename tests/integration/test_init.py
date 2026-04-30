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
    config_text = (root / "config.toml").read_text(encoding="utf-8")
    assert "[embedding]" in config_text
    assert "[retrieval]" in config_text
    assert "[import]" in config_text
    assert 'default_mode = "hybrid"' in config_text
    assert "rrf_k = 60" in config_text
    assert "top_per_path = 50" in config_text
    assert "final_top = 5" in config_text
    assert "sql_shortcut_enabled = true" in config_text
    assert "batch_size = 50" in config_text
    assert "auto_reindex = true" in config_text
    assert "cost_confirm_threshold_usd = 1.0" in config_text
    config = load_config(root / "config.toml")
    assert config.paths.brain_root == root.resolve()
    assert config.openai is None
    assert config.anthropic is None
    assert config.deepseek is not None
    assert config.deepseek.api_key_env == "DEEPSEEK_API_KEY"
    assert config.deepseek.base_url == "https://api.deepseek.com"
    assert config.deepseek.model == "deepseek-v4-pro"
    assert config.deepseek.fast_model == "deepseek-v4-flash"
    assert config.embedding.provider == "openai_compatible"
    assert config.embedding.base_url == "https://api.openai.com/v1"
    assert config.embedding.model == "text-embedding-3-small"
    assert config.embedding.dimension == 1536
    assert config.embedding.api_key_env == "OPENAI_API_KEY"
    assert config.embedding.batch_size == 100
    assert config.embedding.chunk_max_chars == 1500
    assert config.embedding.unit_cost_per_1m_tokens == 0.02
    assert config.retrieval.default_mode == "hybrid"
    assert config.retrieval.rrf_k == 60
    assert config.retrieval.top_per_path == 50
    assert config.retrieval.final_top == 5
    assert config.retrieval.sql_shortcut_enabled is True
    assert config.import_.batch_size == 50
    assert config.import_.auto_reindex is True
    assert config.import_.cost_confirm_threshold_usd == 1.0
    assert _db_user_version(root / "brain.db") == 2
    assert _git_log_messages(root) == ["Initialize brain repository"]
    assert _git_status(root) == []


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
    assert _db_user_version(root / "brain.db") == 2
    assert _git_log_messages(root) == ["Initialize brain repository"]
    assert _git_status(root) == []


def test_init_cli_preserves_version_and_initializes_root(tmp_path: Path) -> None:
    root = tmp_path / "brain"

    version_result = runner.invoke(app, ["--version"])
    command_result = runner.invoke(app, ["init", "--root", str(root)])

    assert version_result.exit_code == 0
    assert version_result.stdout.strip() == "0.2.0"
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


def _git_status(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--short"],
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
