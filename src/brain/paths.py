from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BrainPaths:
    """Resolved filesystem paths for a brain root."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser())

    @property
    def config_path(self) -> Path:
        """Path to config.toml."""
        return self.root / "config.toml"

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database."""
        return self.root / "brain.db"

    @property
    def events_jsonl(self) -> Path:
        """Path to the append-only event ledger."""
        return self.root / "events.jsonl"

    @property
    def claude_md(self) -> Path:
        """Path to the LLM-facing schema notes."""
        return self.root / "CLAUDE.md"

    @property
    def readme_md(self) -> Path:
        """Path to the user-facing README."""
        return self.root / "README.md"

    @property
    def gitignore(self) -> Path:
        """Path to the brain repository .gitignore."""
        return self.root / ".gitignore"

    @property
    def gitattributes(self) -> Path:
        """Path to the brain repository .gitattributes."""
        return self.root / ".gitattributes"

    @property
    def brain_log(self) -> Path:
        """Path to the default log file."""
        return self.root / "brain.log"

    @property
    def raw_dir(self) -> Path:
        """Directory for immutable raw source materials."""
        return self.root / "raw"

    @property
    def laundry_dir(self) -> Path:
        """Directory for unprocessed captured materials."""
        return self.root / "laundry"

    @property
    def laundry_processed_dir(self) -> Path:
        """Directory for archived laundry items."""
        return self.laundry_dir / "processed"

    @property
    def pages_dir(self) -> Path:
        """Directory containing markdown wiki pages."""
        return self.root / "pages"

    @property
    def pages_index(self) -> Path:
        """Path to the generated page index."""
        return self.pages_dir / "index.md"

    @property
    def pages_log(self) -> Path:
        """Path to the append-only global activity log."""
        return self.pages_dir / "log.md"

    @property
    def entities_dir(self) -> Path:
        """Directory for entity pages."""
        return self.pages_dir / "entities"

    @property
    def projects_dir(self) -> Path:
        """Directory for project pages."""
        return self.pages_dir / "projects"

    @property
    def concepts_dir(self) -> Path:
        """Directory for concept pages."""
        return self.pages_dir / "concepts"

    @property
    def events_dir(self) -> Path:
        """Directory for event pages."""
        return self.pages_dir / "events"

    @property
    def experiences_dir(self) -> Path:
        """Directory for experience pages."""
        return self.pages_dir / "experiences"

    @property
    def conversations_dir(self) -> Path:
        """Directory for conversation pages."""
        return self.pages_dir / "conversations"

    @property
    def review_dir(self) -> Path:
        """Directory containing pending review items."""
        return self.root / "review"

    @property
    def review_archive_dir(self) -> Path:
        """Directory containing archived review items."""
        return self.review_dir / "archive"
