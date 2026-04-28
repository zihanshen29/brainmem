from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import frontmatter

import brain.git_ops as git_ops
from brain.config import load_config
from brain.exceptions import BrainError
from brain.models import PageType
from brain.pages import parse_page
from brain.paths import BrainPaths


@dataclass(frozen=True)
class StatusReport:
    """Read-only summary of a brain repository."""

    brain_root: Path
    pages_by_type: dict[str, int]
    entities_by_tier: dict[str, int]
    facts_active: int
    facts_superseded: int
    events_count: int
    pending_reviews: int
    last_ingest_at: str | None
    git_dirty: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "brain_root": str(self.brain_root),
            "pages_by_type": dict(self.pages_by_type),
            "entities_by_tier": dict(self.entities_by_tier),
            "facts_active": self.facts_active,
            "facts_superseded": self.facts_superseded,
            "events_count": self.events_count,
            "pending_reviews": self.pending_reviews,
            "last_ingest_at": self.last_ingest_at,
            "git_dirty": self.git_dirty,
        }


def collect_status(brain_root: Path) -> StatusReport:
    """Collect a read-only repository status report."""
    paths = BrainPaths(Path(brain_root).expanduser().resolve())
    _validate_brain_root(paths)
    load_config(paths.config_path)

    with _connect_readonly(paths.db_path) as conn:
        entities_by_tier = _entities_by_tier(conn)
        facts_active = _count(
            conn,
            "SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL AND valid_to IS NULL",
        )
        facts_superseded = _count(
            conn,
            "SELECT COUNT(*) FROM facts WHERE superseded_by IS NOT NULL OR valid_to IS NOT NULL",
        )
        last_ingest_at = _last_ingest_at(conn)

    return StatusReport(
        brain_root=paths.root,
        pages_by_type=_pages_by_type(paths),
        entities_by_tier=entities_by_tier,
        facts_active=facts_active,
        facts_superseded=facts_superseded,
        events_count=_events_count(paths.events_jsonl),
        pending_reviews=_pending_reviews(paths.review_dir),
        last_ingest_at=last_ingest_at,
        git_dirty=git_ops.is_dirty(paths.root),
    )


def _validate_brain_root(paths: BrainPaths) -> None:
    required_files = [paths.config_path, paths.db_path, paths.events_jsonl]
    required_dirs = [paths.pages_dir, paths.review_dir]
    for path in required_files:
        if not path.is_file():
            raise BrainError(f"Required brain file not found: {path}")
    for path in required_dirs:
        if not path.is_dir():
            raise BrainError(f"Required brain directory not found: {path}")


def _connect_readonly(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error as exc:
        raise BrainError(f"Could not open database read-only: {path}") from exc
    return conn


def _pages_by_type(paths: BrainPaths) -> dict[str, int]:
    counts = {page_type.value: 0 for page_type in PageType}
    for path in sorted(paths.pages_dir.rglob("*.md")):
        if path in {paths.pages_index, paths.pages_log}:
            continue
        page = parse_page(path)
        counts[page.frontmatter.type.value] += 1
    return counts


def _entities_by_tier(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {"1": 0, "2": 0, "3": 0}
    try:
        rows = conn.execute(
            "SELECT tier, COUNT(*) AS count FROM entities GROUP BY tier ORDER BY tier"
        ).fetchall()
    except sqlite3.Error as exc:
        raise BrainError("Could not count entities by tier") from exc

    for row in rows:
        counts[str(row["tier"])] = int(row["count"])
    return counts


def _count(conn: sqlite3.Connection, sql: str) -> int:
    try:
        return int(conn.execute(sql).fetchone()[0])
    except sqlite3.Error as exc:
        raise BrainError("Could not query status counts") from exc


def _last_ingest_at(conn: sqlite3.Connection) -> str | None:
    try:
        value = conn.execute("SELECT MAX(last_run_at) FROM ingest_cursor").fetchone()[0]
    except sqlite3.Error as exc:
        raise BrainError("Could not query ingest cursor") from exc
    return str(value) if value is not None else None


def _events_count(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError as exc:
        raise BrainError(f"Could not read events ledger: {path}") from exc


def _pending_reviews(path: Path) -> int:
    try:
        candidates = sorted(path.glob("*.md"))
    except OSError as exc:
        raise BrainError(f"Could not read review directory: {path}") from exc

    pending = 0
    for review_path in candidates:
        try:
            post = frontmatter.load(review_path, encoding="utf-8")
        except Exception as exc:  # pragma: no cover - parser exposes multiple errors
            raise BrainError(f"Could not read review file: {review_path}") from exc
        if str(post.metadata.get("status", "pending")) == "pending":
            pending += 1
    return pending
