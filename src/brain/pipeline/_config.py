import os
from pathlib import Path

from brain.config import (
    Config,
    GitConfig,
    IngestConfig,
    LintConfig,
    OpenAIConfig,
    PathsConfig,
    TierConfig,
    load_config,
)
from brain.exceptions import ConfigError

BRAIN_CONFIG_ENV = "BRAIN_CONFIG"
DEFAULT_AUTO_ACCEPT = 0.85
DEFAULT_AUTO_REJECT = 0.50


def resolve_pipeline_config(config: Config | None) -> Config:
    """Return explicit config, configured file config, or conservative defaults."""
    if config is not None:
        return config

    env_path = os.environ.get(BRAIN_CONFIG_ENV)
    if env_path:
        return _load_or_default(Path(env_path))

    cwd_config = Path.cwd() / "config.toml"
    if cwd_config.exists():
        return _load_or_default(cwd_config)

    return default_pipeline_config()


def _load_or_default(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError:
        return default_pipeline_config()


def default_pipeline_config() -> Config:
    """Build the fallback config used by isolated pipeline unit tests."""
    return Config(
        openai=OpenAIConfig(
            api_key_env="OPENAI_API_KEY",
            model="gpt-5.5",
            fast_model="gpt-5.4-mini",
        ),
        paths=PathsConfig(brain_root=Path.cwd()),
        ingest=IngestConfig(
            confidence_auto_accept=DEFAULT_AUTO_ACCEPT,
            confidence_auto_reject=DEFAULT_AUTO_REJECT,
        ),
        tier=TierConfig(tier3_threshold=1, tier2_threshold=3, tier1_threshold=8),
        lint=LintConfig(stale_days=90),
        git=GitConfig(auto_commit=False),
    )
