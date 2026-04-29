import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from brain.exceptions import ConfigError


class AnthropicConfig(BaseModel):
    """Anthropic model and key reference settings."""

    model_config = ConfigDict(extra="forbid")

    api_key_env: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    fast_model: str = Field(..., min_length=1)


class OpenAIConfig(BaseModel):
    """OpenAI model and key reference settings."""

    model_config = ConfigDict(extra="forbid")

    api_key_env: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    fast_model: str = Field(..., min_length=1)


class DeepSeekConfig(BaseModel):
    """DeepSeek model, endpoint, and key reference settings."""

    model_config = ConfigDict(extra="forbid")

    api_key_env: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    fast_model: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)


class PathsConfig(BaseModel):
    """User-configurable filesystem paths."""

    model_config = ConfigDict(extra="forbid")

    brain_root: Path

    @field_validator("brain_root", mode="before")
    @classmethod
    def expand_brain_root(cls, value: str | Path) -> Path:
        """Expand a user home marker without resolving the path."""
        return Path(value).expanduser()


class IngestConfig(BaseModel):
    """Confidence thresholds used by ingest."""

    model_config = ConfigDict(extra="forbid")

    confidence_auto_accept: float = Field(..., ge=0.0, le=1.0)
    confidence_auto_reject: float = Field(..., ge=0.0, le=1.0)


class TierConfig(BaseModel):
    """Mention thresholds for tier proposals."""

    model_config = ConfigDict(extra="forbid")

    tier3_threshold: int = Field(..., ge=0)
    tier2_threshold: int = Field(..., ge=0)
    tier1_threshold: int = Field(..., ge=0)


class LintConfig(BaseModel):
    """Lint behavior settings."""

    model_config = ConfigDict(extra="forbid")

    stale_days: int = Field(..., ge=1)


class GitConfig(BaseModel):
    """Git integration settings."""

    model_config = ConfigDict(extra="forbid")

    auto_commit: bool


class Config(BaseModel):
    """Full brain configuration loaded from config.toml."""

    model_config = ConfigDict(extra="forbid")

    openai: OpenAIConfig | None = None
    anthropic: AnthropicConfig | None = None
    deepseek: DeepSeekConfig | None = None
    paths: PathsConfig
    ingest: IngestConfig
    tier: TierConfig
    lint: LintConfig
    git: GitConfig

    @model_validator(mode="after")
    def require_llm_provider(self) -> "Config":
        """Require at least one configured LLM provider."""
        if self.openai is None and self.anthropic is None and self.deepseek is None:
            raise ValueError("At least one LLM provider config is required")
        return self


def load_config(path: Path) -> Config:
    """Load and validate a brain config.toml file.

    Args:
        path: Path to the TOML config file.

    Returns:
        Parsed and validated configuration.

    Raises:
        ConfigError: If the file is missing, invalid TOML, or fails schema validation.
    """
    config_path = Path(path).expanduser()
    try:
        with config_path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in config file: {config_path}") from exc

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config file: {config_path}") from exc
