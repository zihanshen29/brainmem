from __future__ import annotations

import hashlib
import importlib
import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter
import ulid
from pydantic import BaseModel, ConfigDict, Field

from brain.config import load_config
from brain.db.connection import connect, sqlite_uri
from brain.exceptions import BrainError
from brain.import_.cost import cost_estimate
from brain.ledger import append_event
from brain.models import Event, EventKind
from brain.models.import_job import CostEstimate
from brain.paths import BrainPaths

DEFAULT_KINDS = {"md", "txt", "pdf", "jsonl"}
SUPPORTED_SUFFIXES = {".md": "md", ".txt": "txt", ".pdf": "pdf", ".jsonl": "jsonl"}
MAX_SINGLE_DOC_CHARS = 8000
LAUNDRY_KIND_BY_EVENT_KIND = {
    EventKind.RAW_IMPORTED: "note",
    EventKind.AI_CHAT: "ai_chat",
    EventKind.HUMAN_CHAT: "human_chat",
}


class ImportReport(BaseModel):
    """Summary of one bulk import run."""

    model_config = ConfigDict(extra="forbid")

    job_id: str | None = None
    files_found: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    laundry: int = 0
    laundry_dir: str | None = None
    status: str | None = None
    progress_completed: int = 0
    progress_total: int = 0
    estimated_docs: int = 0
    estimated_tokens: int = 0
    estimated_usd: float = 0.0
    dry_run: bool = False
    errors: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _DiscoveredFile:
    path: Path
    relative_path: str
    kind: str
    file_hash: str
    size: int


@dataclass(frozen=True)
class _ExtractedDocument:
    title: str
    content: str
    metadata: dict[str, Any]
    suggested_kind: EventKind = EventKind.RAW_IMPORTED


@dataclass(frozen=True)
class ImportProgress:
    """Core progress event for callers that want UI feedback."""

    job_id: str
    file_path: str | None
    completed: int
    total: int
    status: str


def import_path(
    brain_root: Path,
    source_path: Path | str | None = None,
    kinds: list[str] | set[str] | None = None,
    dry_run: bool = False,
    resume: bool = False,
    yes: bool = False,
    batch_size: int | None = None,
    confirm_callback: Callable[[CostEstimate, float], bool] | None = None,
    progress_callback: Callable[[ImportProgress], None] | None = None,
) -> ImportReport:
    """Import markdown/text files into laundry without running ingest."""
    paths = BrainPaths(Path(brain_root))
    config = load_config(paths.config_path)
    normalized_kinds = _normalize_kinds(kinds)
    effective_batch_size = batch_size or config.import_.batch_size
    if effective_batch_size <= 0:
        raise BrainError("batch_size must be positive")

    if resume:
        return _resume_import(
            paths,
            dry_run=dry_run,
            batch_size=effective_batch_size,
            progress_callback=progress_callback,
        )
    if source_path is None:
        raise BrainError("Import path is required unless --resume is set")

    source = Path(source_path).expanduser()
    source_root = _source_root(source)
    files = _discover_files(source, normalized_kinds)
    estimated_docs = sum(_estimate_docs(file.path) for file in files)
    initial_estimate = cost_estimate(files)
    estimated_tokens = initial_estimate.estimated_extraction_tokens + initial_estimate.estimated_embedding_tokens

    if dry_run:
        return ImportReport(
            files_found=len(files),
            progress_total=len(files),
            estimated_docs=estimated_docs,
            estimated_tokens=estimated_tokens,
            estimated_usd=initial_estimate.estimated_total_usd,
            status="dry-run",
            dry_run=True,
        )

    conn = connect(paths.db_path)
    try:
        importable, skipped = _filter_previously_imported(conn, files)
        estimate = cost_estimate(importable)
        _confirm_cost_if_needed(
            estimate,
            threshold_usd=config.import_.cost_confirm_threshold_usd,
            yes=yes,
            confirm_callback=confirm_callback,
        )
        job_id = str(ulid.ULID())
        laundry_dir = paths.laundry_dir / f"import-{job_id}"
        _create_job(conn, job_id, source, len(importable), estimate)
        _create_file_rows(conn, job_id, importable)
        report = ImportReport(
            job_id=job_id,
            files_found=len(files),
            skipped=skipped,
            estimated_docs=estimated_docs,
            estimated_tokens=estimate.estimated_extraction_tokens + estimate.estimated_embedding_tokens,
            estimated_usd=estimate.estimated_total_usd,
            laundry_dir=laundry_dir.relative_to(paths.root).as_posix(),
            status="running",
            progress_total=len(importable),
            dry_run=False,
        )
        _process_job_files(
            conn=conn,
            paths=paths,
            source_root=source_root,
            job_id=job_id,
            files=importable,
            report=report,
            batch_size=effective_batch_size,
            progress_callback=progress_callback,
        )
        _finish_job(conn, job_id, report)
        return report
    finally:
        _close_connection(conn, paths.db_path)


def _resume_import(
    paths: BrainPaths,
    *,
    dry_run: bool,
    batch_size: int,
    progress_callback: Callable[[ImportProgress], None] | None,
) -> ImportReport:
    conn = _readonly_connection(paths.db_path) if dry_run else connect(paths.db_path)
    try:
        job = _latest_unfinished_job(conn)
        if job is None:
            raise BrainError("No unfinished import job to resume")

        job_id = str(job["id"])
        source_root = _source_root(Path(str(job["source_path"])))
        rows = _pending_file_rows(conn, job_id)
        files = [
            _DiscoveredFile(
                path=source_root / str(row["file_path"]),
                relative_path=str(row["file_path"]),
                kind=str(row["kind"]),
                file_hash=str(row["file_hash"]),
                size=(source_root / str(row["file_path"])).stat().st_size
                if (source_root / str(row["file_path"])).exists()
                else 0,
            )
            for row in rows
        ]
        estimated_docs = sum(_estimate_docs(file.path) for file in files if file.path.exists())
        estimate = cost_estimate(files)
        estimated_tokens = estimate.estimated_extraction_tokens + estimate.estimated_embedding_tokens
        report = ImportReport(
            job_id=job_id,
            files_found=len(files),
            estimated_docs=estimated_docs,
            estimated_tokens=estimated_tokens,
            estimated_usd=estimate.estimated_total_usd,
            laundry_dir=(paths.laundry_dir / f"import-{job_id}").relative_to(paths.root).as_posix(),
            status=str(job["status"]),
            progress_total=len(files),
            dry_run=dry_run,
        )
        if dry_run:
            return report

        _set_job_status(conn, job_id, "running")
        _process_job_files(
            conn=conn,
            paths=paths,
            source_root=source_root,
            job_id=job_id,
            files=files,
            report=report,
            batch_size=batch_size,
            progress_callback=progress_callback,
        )
        _finish_job(conn, job_id, report)
        return report
    finally:
        _close_connection(conn, paths.db_path)


def _process_job_files(
    *,
    conn: sqlite3.Connection,
    paths: BrainPaths,
    source_root: Path,
    job_id: str,
    files: list[_DiscoveredFile],
    report: ImportReport,
    batch_size: int,
    progress_callback: Callable[[ImportProgress], None] | None = None,
) -> None:
    for index, file in enumerate(files, start=1):
        laundry_paths: list[Path] = []
        try:
            documents = _extract_documents(file.path)
            for sequence, document in enumerate(documents, start=1):
                laundry_paths.append(_write_laundry_document(paths, job_id, file.path, document, sequence))
            _mark_file_extracted(conn, job_id, file, laundry_paths)
            _append_bulk_imported_event(paths, job_id, file, source_root)
            report.processed += 1
            report.laundry += len(laundry_paths)
            report.progress_completed = index
            _notify_progress(progress_callback, job_id, file.relative_path, index, len(files), "extracted")
        except KeyboardInterrupt:
            _cleanup_laundry_paths(laundry_paths)
            _mark_file_failed(conn, job_id, file, KeyboardInterrupt())
            _set_job_status(conn, job_id, "paused")
            report.status = "paused"
            raise
        except Exception as exc:
            _cleanup_laundry_paths(laundry_paths)
            _mark_file_failed(conn, job_id, file, exc)
            report.failed += 1
            report.errors.append(f"{file.path}: {exc}")
            report.progress_completed = index
            _notify_progress(progress_callback, job_id, file.relative_path, index, len(files), "failed")

        if index % batch_size == 0:
            conn.commit()
    conn.commit()


def _confirm_cost_if_needed(
    estimate: CostEstimate,
    *,
    threshold_usd: float,
    yes: bool,
    confirm_callback: Callable[[CostEstimate, float], bool] | None,
) -> None:
    if yes or estimate.estimated_total_usd <= threshold_usd:
        return
    if confirm_callback is None:
        raise BrainError(
            "Import cost estimate exceeds confirmation threshold; pass yes=True or provide confirm_callback"
        )
    if not confirm_callback(estimate, threshold_usd):
        raise BrainError("Import cancelled")


def _notify_progress(
    progress_callback: Callable[[ImportProgress], None] | None,
    job_id: str,
    file_path: str | None,
    completed: int,
    total: int,
    status: str,
) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(
            ImportProgress(
                job_id=job_id,
                file_path=file_path,
                completed=completed,
                total=total,
                status=status,
            )
        )
    except Exception:
        return


def _discover_files(source: Path, kinds: set[str]) -> list[_DiscoveredFile]:
    if not source.exists():
        raise BrainError(f"Import path not found: {source}")
    try:
        from brain.import_.discovery import discover_files  # type: ignore[import-not-found]
    except ImportError:
        return _fallback_discover_files(source, kinds)

    discovered = []
    for item in discover_files(source, kinds):
        path = Path(getattr(item, "path", getattr(item, "file_path", item)))
        kind = str(getattr(item, "kind", SUPPORTED_SUFFIXES.get(path.suffix.lower(), "")))
        if kind not in kinds:
            continue
        discovered.append(
            _DiscoveredFile(
                path=path,
                relative_path=str(getattr(item, "relative_path", path.name)),
                kind=kind,
                file_hash=str(getattr(item, "file_hash", _file_hash(path))),
                size=int(getattr(item, "size", path.stat().st_size)),
            )
        )
    return sorted(discovered, key=lambda file: str(file.path).lower())


def _fallback_discover_files(source: Path, kinds: set[str]) -> list[_DiscoveredFile]:
    if not source.exists():
        raise BrainError(f"Import path not found: {source}")
    root = source.resolve()
    candidates = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    files: list[_DiscoveredFile] = []
    for path in candidates:
        kind = SUPPORTED_SUFFIXES.get(path.suffix.lower())
        if kind is None or kind not in kinds:
            continue
        files.append(
            _DiscoveredFile(
                path=path.resolve(),
                relative_path=path.relative_to(root).as_posix() if root.is_dir() else path.name,
                kind=kind,
                file_hash=_file_hash(path),
                size=path.stat().st_size,
            )
        )
    return files


def _extract_documents(path: Path) -> list[_ExtractedDocument]:
    try:
        extractor = _extractor_for_path(path)
    except ImportError:
        return _fallback_extract_documents(path)

    documents = []
    for document in extractor.extract(path):
        documents.append(
            _ExtractedDocument(
                title=str(getattr(document, "title", path.stem)),
                content=str(document.content),
                metadata=dict(getattr(document, "metadata", {})),
                suggested_kind=getattr(document, "suggested_kind", EventKind.RAW_IMPORTED),
            )
        )
    return documents


def _extractor_for_path(path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return importlib.import_module("brain.import_.extractors.markdown").MarkdownExtractor()
    if suffix == ".pdf":
        return importlib.import_module("brain.import_.extractors.pdf").PdfExtractor()
    if suffix == ".jsonl":
        return importlib.import_module("brain.import_.extractors.jsonl").JsonlExtractor()
    raise BrainError(f"Unsupported import suffix: {suffix}")


def _fallback_extract_documents(path: Path) -> list[_ExtractedDocument]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    post = frontmatter.loads(text)
    content = post.content
    title = str(post.metadata.get("title") or _title_from_content(content) or path.stem)
    metadata = dict(post.metadata)
    metadata["original_path"] = str(path)

    if len(content) <= MAX_SINGLE_DOC_CHARS:
        return [_ExtractedDocument(title=title, content=content, metadata=metadata)]
    return _split_by_heading(title, content, metadata)


def _split_by_heading(title: str, content: str, metadata: dict[str, Any]) -> list[_ExtractedDocument]:
    heading_matches = list(re.finditer(r"(?m)^#{1,6}\s+.+$", content))
    if len(heading_matches) <= 1:
        return [
            _ExtractedDocument(
                title=f"{title} part {index + 1}",
                content=content[index : index + MAX_SINGLE_DOC_CHARS],
                metadata={**metadata, "part": index // MAX_SINGLE_DOC_CHARS + 1},
            )
            for index in range(0, len(content), MAX_SINGLE_DOC_CHARS)
        ]

    documents: list[_ExtractedDocument] = []
    preface = content[: heading_matches[0].start()].strip()
    for index, match in enumerate(heading_matches):
        end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(content)
        section = content[match.start() : end].strip()
        if index == 0 and preface:
            section = f"{preface}\n\n{section}"
        if not section:
            continue
        heading = match.group(0).lstrip("#").strip()
        documents.append(
            _ExtractedDocument(
                title=heading or f"{title} part {index + 1}",
                content=section,
                metadata={**metadata, "part": index + 1},
            )
        )
    return documents or [_ExtractedDocument(title=title, content=content, metadata=metadata)]


def _write_laundry_document(
    paths: BrainPaths,
    job_id: str,
    source_path: Path,
    document: _ExtractedDocument,
    sequence: int,
) -> Path:
    imported = _now_utc()
    metadata = {
        "captured": imported.isoformat(),
        "imported": imported.isoformat(),
        "kind": _laundry_kind(document.suggested_kind),
        "source": "import",
        "source_ref": str(source_path),
        "import_job_id": job_id,
        "title": document.title,
    }
    metadata.update({f"import_{key}": value for key, value in document.metadata.items() if key != "title"})

    safe_stem = _safe_filename(document.title or source_path.stem)
    filename = f"{sequence:04d}-{safe_stem}.md"
    path = _unique_path(paths.laundry_dir / f"import-{job_id}" / filename)
    temp_path = _unique_path(path.with_name(f".{path.name}.tmp"))
    body = frontmatter.dumps(frontmatter.Post(document.content.strip(), **metadata), sort_keys=False)
    try:
        _write_lf(temp_path, body)
        _replace_laundry_temp(temp_path, path)
    except Exception:
        _cleanup_laundry_paths([temp_path, path])
        raise
    return path


def _append_bulk_imported_event(
    paths: BrainPaths,
    job_id: str,
    file: _DiscoveredFile,
    source_root: Path,
) -> None:
    append_event(
        paths.events_jsonl,
        Event(
            id=str(ulid.ULID()),
            timestamp=_now_utc(),
            kind=EventKind.BULK_IMPORTED,
            source_ref=file.relative_path,
            metadata={
                "job_id": job_id,
                "source_kind": file.kind,
                "original_path": str((source_root / file.relative_path).resolve()),
            },
        ),
    )


def _cleanup_laundry_paths(paths: list[Path]) -> None:
    for path in paths:
        with suppress(FileNotFoundError):
            path.unlink()


def _replace_laundry_temp(temp_path: Path, target_path: Path) -> None:
    temp_path.replace(target_path)


def _filter_previously_imported(
    conn: sqlite3.Connection,
    files: list[_DiscoveredFile],
) -> tuple[list[_DiscoveredFile], int]:
    importable: list[_DiscoveredFile] = []
    skipped = 0
    for file in files:
        rows = conn.execute(
            """
            SELECT import_files.status, import_files.error, import_jobs.metadata
            FROM import_files
            JOIN import_jobs ON import_jobs.id = import_files.job_id
            WHERE import_files.file_hash = ?
            """,
            (file.file_hash,),
        ).fetchall()
        if not any(_is_duplicate_import_row(row) for row in rows):
            importable.append(file)
        else:
            skipped += 1
    return importable, skipped


def _is_duplicate_import_row(row: sqlite3.Row) -> bool:
    status = str(row["status"])
    if status in {"extracted", "ingested"}:
        return True
    metadata = _metadata_json(str(row["metadata"] or "{}"))
    return not (metadata.get("aborted") and status in {"pending", "failed", "skipped"})


def _create_job(
    conn: sqlite3.Connection,
    job_id: str,
    source: Path,
    total_files: int,
    estimate: CostEstimate,
) -> None:
    conn.execute(
        """
        INSERT INTO import_jobs (
            id, source_path, started_at, status, total_files, estimated_tokens, estimated_usd, metadata
        )
        VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (
            job_id,
            str(source),
            _now_utc().isoformat(),
            total_files,
            estimate.estimated_extraction_tokens + estimate.estimated_embedding_tokens,
            estimate.estimated_total_usd,
            json.dumps({"kinds_breakdown": estimate.by_kind}, sort_keys=True),
        ),
    )
    conn.commit()


def _create_file_rows(conn: sqlite3.Connection, job_id: str, files: list[_DiscoveredFile]) -> None:
    conn.executemany(
        """
        INSERT INTO import_files (job_id, file_path, file_hash, kind, status)
        VALUES (?, ?, ?, ?, 'pending')
        """,
        [(job_id, file.relative_path, file.file_hash, file.kind) for file in files],
    )
    conn.commit()


def _mark_file_extracted(
    conn: sqlite3.Connection,
    job_id: str,
    file: _DiscoveredFile,
    laundry_paths: list[Path],
) -> None:
    conn.execute(
        """
        UPDATE import_files
        SET status = 'extracted',
            laundry_path = ?,
            error = NULL,
            processed_at = ?
        WHERE job_id = ? AND file_path = ?
        """,
        (
            "\n".join(str(path) for path in laundry_paths),
            _now_utc().isoformat(),
            job_id,
            file.relative_path,
        ),
    )
    _refresh_job_counts(conn, job_id)


def _mark_file_failed(
    conn: sqlite3.Connection,
    job_id: str,
    file: _DiscoveredFile,
    exc: Exception,
) -> None:
    conn.execute(
        """
        UPDATE import_files
        SET status = 'failed',
            error = ?,
            processed_at = ?
        WHERE job_id = ? AND file_path = ?
        """,
        (f"{type(exc).__name__}: {exc}", _now_utc().isoformat(), job_id, file.relative_path),
    )
    _refresh_job_counts(conn, job_id)


def _finish_job(conn: sqlite3.Connection, job_id: str, report: ImportReport) -> None:
    status = "completed" if report.failed == 0 else "failed"
    report.status = status
    report.progress_completed = report.progress_total
    conn.execute(
        """
        UPDATE import_jobs
        SET status = ?,
            finished_at = ?,
            processed_files = ?,
            failed_files = ?
        WHERE id = ?
        """,
        (status, _now_utc().isoformat(), report.processed, report.failed, job_id),
    )
    conn.commit()


def _refresh_job_counts(conn: sqlite3.Connection, job_id: str) -> None:
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status IN ('extracted', 'ingested', 'failed', 'skipped') THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
        FROM import_files
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    conn.execute(
        "UPDATE import_jobs SET processed_files = ?, failed_files = ? WHERE id = ?",
        (row[0] or 0, row[1] or 0, job_id),
    )


def _set_job_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    conn.execute("UPDATE import_jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()


def _latest_unfinished_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    rows = conn.execute(
        """
        SELECT *
        FROM import_jobs
        WHERE status IN ('running', 'paused', 'failed')
        ORDER BY started_at DESC
        """
    ).fetchall()
    for row in rows:
        if not _job_metadata(row).get("aborted"):
            return row
    return None


def _pending_file_rows(conn: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM import_files
        WHERE job_id = ?
          AND status IN ('pending', 'failed')
        ORDER BY file_path
        """,
        (job_id,),
    ).fetchall()


def _job_metadata(row: sqlite3.Row) -> dict[str, Any]:
    return _metadata_json(str(row["metadata"] or "{}"))


def _metadata_json(value: str) -> dict[str, Any]:
    try:
        metadata = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _normalize_kinds(kinds: list[str] | set[str] | None) -> set[str]:
    if kinds is None:
        return set(DEFAULT_KINDS)
    normalized = {kind.strip().lower().lstrip(".") for kind in kinds if kind.strip()}
    unsupported = normalized - DEFAULT_KINDS
    if unsupported:
        raise BrainError(f"Unsupported import kinds for Task 22: {', '.join(sorted(unsupported))}")
    return normalized or set(DEFAULT_KINDS)


def _source_root(source: Path) -> Path:
    expanded = Path(source).expanduser()
    if expanded.exists() and expanded.is_file():
        return expanded.parent.resolve()
    return expanded.resolve()


def _estimate_docs(path: Path) -> int:
    if path.suffix.lower() in {".pdf", ".jsonl"}:
        return 1
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 1
    if len(text) <= MAX_SINGLE_DOC_CHARS:
        return 1
    headings = re.findall(r"(?m)^#{1,6}\s+.+$", text)
    return max(1, len(headings))


def _title_from_content(content: str) -> str | None:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or None
        if stripped:
            return stripped[:80]
    return None


def _safe_filename(value: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    ascii_name = re.sub(r"-{2,}", "-", ascii_name).strip(".-_")
    return (ascii_name or "imported")[:80]


def _laundry_kind(suggested_kind: EventKind) -> str:
    return LAUNDRY_KIND_BY_EVENT_KIND.get(suggested_kind, "note")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise BrainError(f"Could not find unique laundry filename for {path}")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    path.write_text(normalized, encoding="utf-8", newline="\n")


def _readonly_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(sqlite_uri(path, mode="ro", immutable=1), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _close_connection(conn: sqlite3.Connection, db_path: Path) -> None:
    try:
        if not db_path.exists():
            return
        with suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(f"{db_path.name}{suffix}")
            try:
                sidecar.unlink()
            except (FileNotFoundError, PermissionError):
                continue


def _now_utc() -> datetime:
    return datetime.now(UTC)
