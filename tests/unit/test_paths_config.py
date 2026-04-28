from pathlib import Path

import pytest

from brain.config import load_config
from brain.exceptions import ConfigError
from brain.paths import BrainPaths


def valid_config_text(root: Path) -> str:
    return f"""
[anthropic]
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-opus-4-7"
fast_model = "claude-haiku-4-5"

[paths]
brain_root = "{root.as_posix()}"

[ingest]
confidence_auto_accept = 0.85
confidence_auto_reject = 0.50

[tier]
tier3_threshold = 1
tier2_threshold = 3
tier1_threshold = 8

[lint]
stale_days = 90

[git]
auto_commit = true
""".strip()


def test_brain_paths_expand_from_root(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    paths = BrainPaths(root)

    assert paths.root == root
    assert paths.config_path == root / "config.toml"
    assert paths.events_jsonl == root / "events.jsonl"
    assert paths.db_path == root / "brain.db"
    assert paths.raw_dir == root / "raw"
    assert paths.laundry_dir == root / "laundry"
    assert paths.laundry_processed_dir == root / "laundry" / "processed"
    assert paths.pages_dir == root / "pages"
    assert paths.pages_index == root / "pages" / "index.md"
    assert paths.pages_log == root / "pages" / "log.md"
    assert paths.entities_dir == root / "pages" / "entities"
    assert paths.projects_dir == root / "pages" / "projects"
    assert paths.concepts_dir == root / "pages" / "concepts"
    assert paths.events_dir == root / "pages" / "events"
    assert paths.experiences_dir == root / "pages" / "experiences"
    assert paths.conversations_dir == root / "pages" / "conversations"
    assert paths.review_dir == root / "review"
    assert paths.review_archive_dir == root / "review" / "archive"


def test_brain_paths_expand_home() -> None:
    paths = BrainPaths(Path("~/brain"))

    assert paths.root == Path("~/brain").expanduser()


def test_load_config_valid_file(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    config_path = tmp_path / "config.toml"
    config_path.write_text(valid_config_text(root), encoding="utf-8", newline="\n")

    config = load_config(config_path)

    assert config.anthropic.api_key_env == "ANTHROPIC_API_KEY"
    assert config.anthropic.model == "claude-opus-4-7"
    assert config.anthropic.fast_model == "claude-haiku-4-5"
    assert config.paths.brain_root == root
    assert config.ingest.confidence_auto_accept == 0.85
    assert config.ingest.confidence_auto_reject == 0.50
    assert config.tier.tier3_threshold == 1
    assert config.tier.tier2_threshold == 3
    assert config.tier.tier1_threshold == 8
    assert config.lint.stale_days == 90
    assert config.git.auto_commit is True


def test_load_config_missing_required_field_raises_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[anthropic]
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-opus-4-7"

[paths]
brain_root = "~/brain"
""".strip(),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[anthropic\napi_key_env = 'x'", encoding="utf-8", newline="\n")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")
