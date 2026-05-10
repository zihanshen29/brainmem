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


class ProcedureConfig(BaseModel):
    """Procedure maturity state thresholds."""

    model_config = ConfigDict(extra="forbid")

    stable_success_threshold: int = Field(default=5, ge=1)
    stable_fail_threshold: int = Field(default=2, ge=1)


class LintConfig(BaseModel):
    """Lint behavior settings."""

    model_config = ConfigDict(extra="forbid")

    stale_days: int = Field(..., ge=1)


class GitConfig(BaseModel):
    """Git integration settings."""

    model_config = ConfigDict(extra="forbid")

    auto_commit: bool


class EmbeddingConfig(BaseModel):
    """Embedding provider settings."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="openai_compatible", min_length=1)
    api_key_env: str = Field(default="OPENAI_API_KEY", min_length=1)
    model: str = Field(default="text-embedding-3-small", min_length=1)
    base_url: str = Field(default="https://api.openai.com/v1", min_length=1)
    dimension: int = Field(default=1536, gt=0)
    batch_size: int = Field(default=100, gt=0)
    chunk_max_chars: int = Field(default=1500, gt=0)
    unit_cost_per_1m_tokens: float = Field(default=0.02, ge=0.0)


class RetrievalConfig(BaseModel):
    """Hybrid retrieval settings."""

    model_config = ConfigDict(extra="forbid")

    default_mode: str = Field(default="hybrid", min_length=1)
    rrf_k: int = Field(default=60, gt=0)
    top_per_path: int = Field(default=50, gt=0)
    final_top: int = Field(default=5, gt=0)
    sql_shortcut_enabled: bool = True


class ImportConfig(BaseModel):
    """Bulk import behavior settings."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=50, gt=0)
    auto_reindex: bool = True
    cost_confirm_threshold_usd: float = Field(default=1.0, ge=0.0)


class Config(BaseModel):
    """Full brain configuration loaded from config.toml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    openai: OpenAIConfig | None = None
    anthropic: AnthropicConfig | None = None
    deepseek: DeepSeekConfig | None = None
    paths: PathsConfig
    ingest: IngestConfig
    tier: TierConfig
    procedure: ProcedureConfig = Field(default_factory=ProcedureConfig)
    lint: LintConfig
    git: GitConfig
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    import_: ImportConfig = Field(default_factory=ImportConfig, alias="import")

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
