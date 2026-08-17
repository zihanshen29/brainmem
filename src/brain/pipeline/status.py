from __future__ import annotations

import re
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

import brain.git_ops as git_ops
from brain.config import load_config
from brain.db.connection import sqlite_uri
from brain.exceptions import BrainError
from brain.models import PageType
from brain.pages import parse_page
from brain.paths import BrainPaths
from brain.pipeline.chunking import split_page_into_chunks

_FAILED_LAUNDRY_DIR_NAME = "failed"
_PROCESSED_LAUNDRY_DIR_NAME = "processed"
_MAX_FRONTMATTER_BYTES = 64 * 1024
_REVIEW_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class FileHealth:
    """Content-free health metadata for one optional local file."""

    exists: bool
    updated_at: str | None

    def to_dict(self) -> dict[str, bool | str | None]:
        return {
            "exists": self.exists,
            "updated_at": self.updated_at,
        }


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
    embedding_coverage: dict[str, int | float]
    last_reindex_at: str | None
    active_import_jobs: int
    token_usage: dict[str, int]
    total_cost_usd: float
    pending_reviews_by_kind: dict[str, int] = field(default_factory=dict)
    laundry: dict[str, int] = field(
        default_factory=lambda: {"pending": 0, "failed": 0}
    )
    scratch: dict[str, FileHealth] = field(
        default_factory=lambda: {
            "working": FileHealth(exists=False, updated_at=None),
            "snapshot": FileHealth(exists=False, updated_at=None),
        }
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "brain_root": str(self.brain_root),
            "pages_by_type": dict(self.pages_by_type),
            "entities_by_tier": dict(self.entities_by_tier),
            "facts_active": self.facts_active,
            "facts_superseded": self.facts_superseded,
            "events_count": self.events_count,
            "pending_reviews": self.pending_reviews,
            "pending_reviews_by_kind": dict(self.pending_reviews_by_kind),
            "laundry": dict(self.laundry),
            "scratch": {
                name: health.to_dict() for name, health in self.scratch.items()
            },
            "last_ingest_at": self.last_ingest_at,
            "git_dirty": self.git_dirty,
            "embedding_coverage": dict(self.embedding_coverage),
            "last_reindex_at": self.last_reindex_at,
            "active_import_jobs": self.active_import_jobs,
            "token_usage": dict(self.token_usage),
            "total_cost_usd": self.total_cost_usd,
        }


def collect_status(brain_root: Path) -> StatusReport:
    """Collect a read-only repository status report."""
    paths = BrainPaths(Path(brain_root).expanduser().resolve())
    _validate_brain_root(paths)
    config = load_config(paths.config_path)

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
        indexed_chunks = _embedding_index_count(conn)
        last_reindex_at = _last_reindex_at(conn)
        active_import_jobs = _active_import_jobs(conn)
        token_usage = _token_usage(conn)
        total_cost_usd = _stat_float(conn, "total_cost_usd", default=0.0)

    total_chunks = _page_chunk_count(paths, config.embedding.chunk_max_chars)

    pending_reviews_by_kind = _pending_reviews_by_kind(paths.review_dir)

    return StatusReport(
        brain_root=paths.root,
        pages_by_type=_pages_by_type(paths),
        entities_by_tier=entities_by_tier,
        facts_active=facts_active,
        facts_superseded=facts_superseded,
        events_count=_events_count(paths.events_jsonl),
        pending_reviews=sum(pending_reviews_by_kind.values()),
        pending_reviews_by_kind=pending_reviews_by_kind,
        laundry=_laundry_counts(paths.laundry_dir),
        scratch={
            "working": _file_health(paths.working_buffer),
            "snapshot": _file_health(paths.snapshot_path),
        },
        last_ingest_at=last_ingest_at,
        git_dirty=git_ops.is_dirty(paths.root),
        embedding_coverage=_embedding_coverage(total_chunks, indexed_chunks),
        last_reindex_at=last_reindex_at,
        active_import_jobs=active_import_jobs,
        token_usage=token_usage,
        total_cost_usd=total_cost_usd,
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
        conn = sqlite3.connect(sqlite_uri(path, mode="ro"), uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error as exc:
        raise BrainError(f"Could not open database read-only: {path}") from exc
    return conn


def _pages_by_type(paths: BrainPaths) -> dict[str, int]:
    counts = {page_type.value: 0 for page_type in PageType}
    for path in _iter_page_paths(paths):
        page = parse_page(path)
        counts[page.frontmatter.type.value] += 1
    return counts


def _iter_page_paths(paths: BrainPaths) -> list[Path]:
    return sorted(
        path
        for path in paths.pages_dir.rglob("*.md")
        if path not in {paths.pages_index, paths.pages_log}
    )


def _page_chunk_count(paths: BrainPaths, max_chars: int) -> int:
    total = 0
    for path in _iter_page_paths(paths):
        total += len(split_page_into_chunks(parse_page(path), max_chars))
    return total


def _embedding_coverage(total_chunks: int, indexed_chunks: int) -> dict[str, int | float]:
    covered_chunks = min(indexed_chunks, total_chunks)
    ratio = 1.0 if total_chunks == 0 else covered_chunks / total_chunks
    return {
        "total_chunks": total_chunks,
        "indexed_chunks": indexed_chunks,
        "missing_chunks": max(total_chunks - indexed_chunks, 0),
        "ratio": round(ratio, 6),
    }


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


def _last_reindex_at(conn: sqlite3.Connection) -> str | None:
    value = _stat_value(conn, "last_reindex_at")
    if value is None or not value.strip():
        return None
    return value


def _embedding_index_count(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "embedding_index"):
        return 0
    try:
        return int(conn.execute("SELECT COUNT(*) FROM embedding_index").fetchone()[0])
    except sqlite3.Error:
        return 0


def _active_import_jobs(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "import_jobs"):
        return 0
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM import_jobs WHERE status IN ('running', 'paused')"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        return 0


def _token_usage(conn: sqlite3.Connection) -> dict[str, int]:
    embedding = _stat_int(conn, "total_embedding_tokens", default=0)
    extraction = _stat_int(conn, "total_extraction_tokens", default=0)
    return {
        "embedding": embedding,
        "extraction": extraction,
        "total": embedding + extraction,
    }


def _stat_value(conn: sqlite3.Connection, key: str) -> str | None:
    if not _table_exists(conn, "stats"):
        return None
    try:
        row = conn.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return str(row["value"])


def _stat_int(conn: sqlite3.Connection, key: str, *, default: int) -> int:
    value = _stat_value(conn, key)
    if value is None or not value.strip():
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _stat_float(conn: sqlite3.Connection, key: str, *, default: float) -> float:
    value = _stat_value(conn, key)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND name = ?
            """,
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _events_count(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError as exc:
        raise BrainError(f"Could not read events ledger: {path}") from exc


def _pending_reviews_by_kind(path: Path) -> dict[str, int]:
    try:
        candidates = sorted(path.glob("*.md"))
    except OSError as exc:
        raise BrainError(f"Could not read review directory: {path}") from exc

    counts: dict[str, int] = {}
    for review_path in candidates:
        try:
            metadata = _frontmatter_metadata_only(review_path)
        except Exception as exc:  # pragma: no cover - parser exposes multiple errors
            raise BrainError(f"Could not read review file: {review_path}") from exc
        if str(metadata.get("status", "pending")) != "pending":
            continue
        raw_kind = str(metadata.get("kind") or "").strip()
        kind = raw_kind if _REVIEW_KIND_RE.fullmatch(raw_kind) else "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def _frontmatter_metadata_only(path: Path) -> dict[str, object]:
    """Read bounded review frontmatter without reading the review body."""
    with path.open("rb", buffering=0) as handle:
        first = handle.readline(_MAX_FRONTMATTER_BYTES + 1)
        if len(first) > _MAX_FRONTMATTER_BYTES:
            raise ValueError("review frontmatter is too large")
        if first.removeprefix(b"\xef\xbb\xbf").strip() != b"---":
            return {}

        header_lines = [first]
        total_bytes = len(first)
        while True:
            remaining = _MAX_FRONTMATTER_BYTES - total_bytes
            if remaining <= 0:
                raise ValueError("review frontmatter is too large")
            line = handle.readline(remaining + 1)
            if not line:
                raise ValueError("review frontmatter is not terminated")
            total_bytes += len(line)
            if total_bytes > _MAX_FRONTMATTER_BYTES:
                raise ValueError("review frontmatter is too large")
            header_lines.append(line)
            if line.strip() in {b"---", b"..."}:
                break

    header = b"".join(header_lines).decode("utf-8-sig")
    return dict(frontmatter.loads(header).metadata)


def _laundry_counts(path: Path) -> dict[str, int]:
    counts = {"pending": 0, "failed": 0}
    if not path.exists():
        return counts

    try:
        candidates = sorted(
            candidate for candidate in path.rglob("*") if candidate.is_file()
        )
    except OSError as exc:
        raise BrainError(f"Could not read laundry directory: {path}") from exc

    for candidate in candidates:
        relative = candidate.relative_to(path)
        top_level = relative.parts[0]
        if top_level == _FAILED_LAUNDRY_DIR_NAME:
            counts["failed"] += 1
        elif top_level != _PROCESSED_LAUNDRY_DIR_NAME:
            counts["pending"] += 1
    return counts


def _file_health(path: Path) -> FileHealth:
    try:
        file_stat = path.stat()
    except FileNotFoundError:
        return FileHealth(exists=False, updated_at=None)
    except OSError as exc:
        raise BrainError(f"Could not inspect local status file: {path}") from exc

    if not stat.S_ISREG(file_stat.st_mode):
        return FileHealth(exists=False, updated_at=None)
    updated_at = datetime.fromtimestamp(file_stat.st_mtime, tz=UTC).isoformat()
    return FileHealth(exists=True, updated_at=updated_at)
