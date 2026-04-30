from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import ulid

from brain.import_.discovery import DiscoveredFile
from brain.models.import_job import CostEstimate, ImportFile, ImportFileKind, ImportJob


def create_job(
    conn: sqlite3.Connection,
    source_path: Path,
    *,
    total_files: int,
    estimate: CostEstimate | None = None,
    metadata: dict[str, Any] | None = None,
) -> ImportJob:
    """Create and persist a running import job."""
    job_id = str(ulid.ULID())
    started_at = _now_iso()
    job_metadata = metadata or {}
    if estimate is not None:
        job_metadata = {**job_metadata, "kinds_breakdown": {str(k): v for k, v in estimate.by_kind.items()}}
    conn.execute(
        """
        INSERT INTO import_jobs (
            id, source_path, started_at, status, total_files,
            estimated_tokens, estimated_usd, metadata
        )
        VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (
            job_id,
            str(source_path),
            started_at,
            total_files,
            estimate.estimated_extraction_tokens + estimate.estimated_embedding_tokens if estimate else None,
            estimate.estimated_total_usd if estimate else None,
            json.dumps(job_metadata, sort_keys=True),
        ),
    )
    return get_job(conn, job_id)


def get_job(conn: sqlite3.Connection, job_id: str) -> ImportJob:
    """Return one import job by id."""
    row = conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"Import job not found: {job_id}")
    return _job_from_row(row)


def list_jobs(
    conn: sqlite3.Connection,
    *,
    statuses: set[str] | None = None,
    limit: int | None = None,
) -> list[ImportJob]:
    """List import jobs newest first."""
    params: list[object] = []
    where = ""
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        where = f"WHERE status IN ({placeholders})"
        params.extend(sorted(statuses))
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT *
        FROM import_jobs
        {where}
        ORDER BY started_at DESC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [_job_from_row(row) for row in rows]


def get_job_detail(conn: sqlite3.Connection, job_id: str) -> tuple[ImportJob, list[ImportFile]]:
    """Return a job and all file rows attached to it."""
    return get_job(conn, job_id), list_files(conn, job_id)


def register_files(conn: sqlite3.Connection, job_id: str, files: list[DiscoveredFile]) -> list[ImportFile]:
    """Register discovered files as pending for a job."""
    conn.executemany(
        """
        INSERT INTO import_files (job_id, file_path, file_hash, kind, status)
        VALUES (?, ?, ?, ?, 'pending')
        """,
        [(job_id, file.relative_path, file.file_hash, file.kind) for file in files],
    )
    return list_files(conn, job_id)


def list_files(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    statuses: set[str] | None = None,
) -> list[ImportFile]:
    """List files registered to an import job."""
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT * FROM import_files
            WHERE job_id = ? AND status IN ({placeholders})
            ORDER BY file_path
            """,
            (job_id, *sorted(statuses)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM import_files WHERE job_id = ? ORDER BY file_path",
            (job_id,),
        ).fetchall()
    return [_file_from_row(row) for row in rows]


def mark_extracted(conn: sqlite3.Connection, job_id: str, file_path: str, laundry_path: Path | str) -> ImportFile:
    """Mark a file as extracted into laundry."""
    _update_file_status(conn, job_id, file_path, "extracted", laundry_path=str(laundry_path), error=None)
    _refresh_job_counts(conn, job_id)
    return _get_file(conn, job_id, file_path)


def mark_failed(conn: sqlite3.Connection, job_id: str, file_path: str, error: str) -> ImportFile:
    """Mark a file as failed."""
    _update_file_status(conn, job_id, file_path, "failed", error=error)
    _refresh_job_counts(conn, job_id)
    return _get_file(conn, job_id, file_path)


def mark_skipped(conn: sqlite3.Connection, job_id: str, file_path: str, reason: str) -> ImportFile:
    """Mark a file as skipped."""
    _update_file_status(conn, job_id, file_path, "skipped", error=reason)
    _refresh_job_counts(conn, job_id)
    return _get_file(conn, job_id, file_path)


def complete_job(conn: sqlite3.Connection, job_id: str) -> ImportJob:
    """Mark a job completed."""
    conn.execute(
        "UPDATE import_jobs SET status = 'completed', finished_at = ? WHERE id = ?",
        (_now_iso(), job_id),
    )
    _refresh_job_counts(conn, job_id)
    return get_job(conn, job_id)


def abort_job(conn: sqlite3.Connection, job_id: str) -> ImportJob:
    """Abort a resumable job and mark remaining files so resume ignores them."""
    job = get_job(conn, job_id)
    if job.status == "completed":
        raise ValueError(f"Cannot abort completed import job: {job_id}")

    now = _now_iso()
    metadata = {**job.metadata, "aborted": True}
    conn.execute(
        """
        UPDATE import_files
        SET status = 'skipped',
            error = 'aborted',
            processed_at = ?
        WHERE job_id = ?
          AND status IN ('pending', 'failed')
        """,
        (now, job_id),
    )
    conn.execute(
        """
        UPDATE import_jobs
        SET status = 'failed',
            finished_at = ?,
            metadata = ?
        WHERE id = ?
        """,
        (now, json.dumps(metadata, sort_keys=True), job_id),
    )
    _refresh_job_counts(conn, job_id)
    return get_job(conn, job_id)


def find_latest_unfinished_job(conn: sqlite3.Connection) -> ImportJob | None:
    """Return the newest non-aborted job that can still be resumed."""
    rows = conn.execute(
        """
        SELECT * FROM import_jobs
        WHERE status IN ('running', 'paused', 'failed')
        ORDER BY started_at DESC
        """
    ).fetchall()
    for row in rows:
        job = _job_from_row(row)
        if not job.metadata.get("aborted"):
            return job
    return None


def find_duplicate_file_hashes(
    conn: sqlite3.Connection,
    file_hashes: set[str],
    *,
    exclude_job_id: str | None = None,
) -> dict[str, list[ImportFile]]:
    """Find already registered files with matching hashes across jobs."""
    if not file_hashes:
        return {}
    placeholders = ",".join("?" for _ in file_hashes)
    params: list[object] = [*sorted(file_hashes)]
    where = f"file_hash IN ({placeholders})"
    if exclude_job_id is not None:
        where = f"{where} AND job_id != ?"
        params.append(exclude_job_id)
    rows = conn.execute(f"SELECT * FROM import_files WHERE {where} ORDER BY job_id, file_path", params).fetchall()
    duplicates: dict[str, list[ImportFile]] = {}
    for row in rows:
        file = _file_from_row(row)
        duplicates.setdefault(file.file_hash, []).append(file)
    return duplicates


def _update_file_status(
    conn: sqlite3.Connection,
    job_id: str,
    file_path: str,
    status: str,
    *,
    laundry_path: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE import_files
        SET status = ?, laundry_path = COALESCE(?, laundry_path), error = ?, processed_at = ?
        WHERE job_id = ? AND file_path = ?
        """,
        (status, laundry_path, error, _now_iso(), job_id, file_path),
    )


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


def _get_file(conn: sqlite3.Connection, job_id: str, file_path: str) -> ImportFile:
    row = conn.execute(
        "SELECT * FROM import_files WHERE job_id = ? AND file_path = ?",
        (job_id, file_path),
    ).fetchone()
    if row is None:
        raise ValueError(f"Import file not found: {job_id}/{file_path}")
    return _file_from_row(row)


def _file_from_row(row: sqlite3.Row) -> ImportFile:
    return ImportFile(
        job_id=row["job_id"],
        file_path=row["file_path"],
        file_hash=row["file_hash"],
        kind=_kind(row["kind"]),
        status=row["status"],
        laundry_path=row["laundry_path"],
        error=row["error"],
        processed_at=_parse_datetime(row["processed_at"]),
    )


def _job_from_row(row: sqlite3.Row) -> ImportJob:
    metadata = json.loads(row["metadata"] or "{}")
    return ImportJob(
        id=row["id"],
        source_path=row["source_path"],
        started_at=_parse_datetime(row["started_at"]) or datetime.min,
        finished_at=_parse_datetime(row["finished_at"]),
        status=row["status"],
        total_files=row["total_files"],
        processed_files=row["processed_files"],
        failed_files=row["failed_files"],
        estimated_tokens=row["estimated_tokens"],
        estimated_usd=row["estimated_usd"],
        actual_tokens=row["actual_tokens"],
        metadata=metadata,
    )


def _kind(value: str) -> ImportFileKind:
    if value not in {"md", "txt", "pdf", "jsonl"}:
        raise ValueError(f"Unsupported import file kind in database: {value}")
    return value  # type: ignore[return-value]


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()
