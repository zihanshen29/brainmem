from __future__ import annotations

import sqlite3
from contextlib import closing, suppress
from pathlib import Path
from typing import Annotated, Any

import typer

from brain.db.connection import connect, sqlite_uri
from brain.exceptions import BrainError
from brain.paths import BrainPaths


def import_command(
    path: Annotated[
        Path | None,
        typer.Argument(help="File or directory to import."),
    ] = None,
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="Comma-separated file kinds: md,txt,pdf,jsonl."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview import work without writing DB, events, or laundry."),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Continue the latest running or paused import job."),
    ] = False,
    status: Annotated[
        bool,
        typer.Option("--status", help="Show import job status. Optionally pass job id as the argument."),
    ] = False,
    list_jobs_requested: Annotated[
        bool,
        typer.Option("--list-jobs", help="List recent import jobs."),
    ] = False,
    abort_job_id: Annotated[
        str | None,
        typer.Option("--abort", help="Abort an import job by id."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip confirmation prompts."),
    ] = False,
    batch_size: Annotated[
        int | None,
        typer.Option("--batch-size", help="Commit import progress every N files."),
    ] = None,
) -> None:
    """Bulk import supported files into laundry."""
    try:
        root = Path.cwd() if brain_root is None else brain_root
        if sum([status, list_jobs_requested, abort_job_id is not None]) > 1:
            typer.echo("Error: select only one of --status, --list-jobs, or --abort", err=True)
            raise typer.Exit(1)
        if status:
            typer.echo(_job_status(root, str(path) if path is not None else None))
            return
        if list_jobs_requested:
            typer.echo(_job_list(root))
            return
        if abort_job_id is not None:
            typer.echo(_abort_job(root, abort_job_id))
            return
        report = _run_import(
            root,
            path,
            kinds=_parse_kinds(kind),
            dry_run=dry_run,
            resume=resume,
            yes=yes,
            batch_size=batch_size,
        )
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt as exc:
        typer.echo("Import paused. Re-run `mem import --resume` to continue.", err=True)
        raise typer.Exit(130) from exc

    typer.echo(_summary(report))
    for error in report.errors:
        typer.echo(f"Error: {error}", err=True)


def _run_import(
    brain_root: Path,
    path: Path | None,
    *,
    kinds: list[str] | None,
    dry_run: bool,
    resume: bool,
    yes: bool,
    batch_size: int | None,
) -> Any:
    from brain.import_.importer import ImportProgress, import_path

    def confirm_callback(estimate: Any, threshold_usd: float) -> bool:
        typer.echo(
            "Import cost estimate: "
            f"files={estimate.total_files} "
            f"tokens={estimate.estimated_extraction_tokens + estimate.estimated_embedding_tokens} "
            f"estimated_usd=${estimate.estimated_total_usd:.4f} "
            f"threshold_usd=${threshold_usd:.4f}"
        )
        return typer.confirm("Continue import?", default=False, abort=False)

    if dry_run or not _progress_enabled():
        return import_path(
            brain_root,
            path,
            kinds=kinds,
            dry_run=dry_run,
            resume=resume,
            yes=yes,
            batch_size=batch_size,
            confirm_callback=confirm_callback,
        )

    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

    task_id: int | None = None

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        transient=True,
    ) as progress:

        def progress_callback(update: ImportProgress) -> None:
            nonlocal task_id
            description = f"Importing {update.file_path or update.job_id}"
            if task_id is None:
                task_id = progress.add_task(description, total=update.total)
            progress.update(task_id, completed=update.completed, total=update.total, description=description)

        return import_path(
            brain_root,
            path,
            kinds=kinds,
            dry_run=dry_run,
            resume=resume,
            yes=yes,
            batch_size=batch_size,
            confirm_callback=confirm_callback,
            progress_callback=progress_callback,
        )


def _parse_kinds(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _summary(report: Any) -> str:
    return (
        "Import summary: "
        f"job_id={report.job_id or '-'} "
        f"status={report.status or '-'} "
        f"progress={report.progress_completed}/{report.progress_total} "
        f"files_found={report.files_found} "
        f"processed={report.processed} "
        f"skipped={report.skipped} "
        f"failed={report.failed} "
        f"laundry={report.laundry} "
        f"laundry_dir={report.laundry_dir or '-'} "
        f"estimated_docs={report.estimated_docs} "
        f"estimated_tokens={report.estimated_tokens} "
        f"estimated_usd=${report.estimated_usd:.4f} "
        f"dry_run={str(report.dry_run).lower()}"
    )


def _job_status(brain_root: Path, job_id: str | None) -> str:
    from brain.import_.jobs import get_job_detail, list_jobs

    with closing(_readonly_jobs_connection(brain_root)) as conn:
        if job_id is None:
            jobs = list_jobs(conn, limit=1)
            if not jobs:
                return "No import jobs."
            job_id = jobs[0].id
        try:
            job, files = get_job_detail(conn, job_id)
        except ValueError as exc:
            raise BrainError(str(exc)) from exc

    lines = [
        "Import job:",
        _format_job(job),
        "Files:",
    ]
    if not files:
        lines.append("- none")
    for file in files:
        suffix = f" error={file.error}" if file.error else ""
        lines.append(f"- {file.status} {file.kind} {file.file_path}{suffix}")
    return "\n".join(lines)


def _job_list(brain_root: Path) -> str:
    from brain.import_.jobs import list_jobs

    with closing(_readonly_jobs_connection(brain_root)) as conn:
        jobs = list_jobs(conn, limit=20)
    if not jobs:
        return "No import jobs."
    return "\n".join(["Import jobs:", *[f"- {_format_job(job)}" for job in jobs]])


def _abort_job(brain_root: Path, job_id: str) -> str:
    from brain.import_.jobs import abort_job

    paths = BrainPaths(Path(brain_root).expanduser().resolve())
    conn = connect(paths.db_path)
    try:
        job = abort_job(conn, job_id)
        conn.commit()
        return f"Import job aborted: {_format_job(job)}"
    except ValueError as exc:
        raise BrainError(str(exc)) from exc
    finally:
        _close_write_connection(conn)


def _format_job(job: Any) -> str:
    estimated_usd = "-" if job.estimated_usd is None else f"${job.estimated_usd:.4f}"
    aborted = " aborted=true" if job.metadata.get("aborted") else ""
    return (
        f"job_id={job.id} "
        f"status={job.status} "
        f"progress={job.processed_files}/{job.total_files} "
        f"failed={job.failed_files} "
        f"estimated_usd={estimated_usd} "
        f"source={job.source_path}"
        f"{aborted}"
    )


def _readonly_jobs_connection(brain_root: Path) -> sqlite3.Connection:
    paths = BrainPaths(Path(brain_root).expanduser().resolve())
    try:
        conn = sqlite3.connect(sqlite_uri(paths.db_path, mode="ro", immutable=1), uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise BrainError(f"Could not open import jobs database: {paths.db_path}") from exc
    return conn


def _close_write_connection(conn: sqlite3.Connection) -> None:
    try:
        with suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _progress_enabled() -> bool:
    return bool(getattr(typer.get_text_stream("stdout"), "isatty", lambda: False)())
