class BrainError(Exception):
    """Base for all brain-specific errors."""


class PageParseError(BrainError):
    """Raised when a markdown page cannot be parsed."""


class ConfigError(BrainError):
    """Raised when configuration cannot be loaded or validated."""


class DBError(BrainError):
    """Raised when SQLite operations fail."""


class IngestError(BrainError):
    """Raised when ingest cannot complete."""


class LLMError(BrainError):
    """Raised when LLM operations fail."""


class GitError(BrainError):
    """Raised when git operations fail."""
