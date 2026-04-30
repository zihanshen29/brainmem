from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError


class IngestSource(StrEnum):
    """Supported ingest source selectors."""

    LAUNDRY = "laundry"
    EVENTS = "events"
    ALL = "all"


def ingest_command(
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview ingest work without writing files, database rows, events, cursors, or git commits.",
        ),
    ] = False,
    source: Annotated[
        IngestSource,
        typer.Option(
            "--source",
            help="Source to ingest.",
            case_sensitive=False,
        ),
    ] = IngestSource.ALL,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            help="Maximum number of source items to ingest.",
        ),
    ] = None,
    no_auto_reindex: Annotated[
        bool,
        typer.Option(
            "--no-auto-reindex",
            help="Skip automatic embedding reindex after a successful ingest.",
        ),
    ] = False,
    verbose: Annotated[
        bool | None,
        typer.Option(
            "--verbose/--quiet",
            help="Show detailed report output, or suppress successful output.",
        ),
    ] = None,
) -> None:
    """Ingest captured source material into the current brain repository."""
    try:
        report = _run_ingest(
            Path.cwd() if brain_root is None else brain_root,
            source=source.value,
            dry_run=dry_run,
            limit=limit,
            auto_reindex=False if no_auto_reindex else None,
        )
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if verbose is False:
        return

    typer.echo(_summary(report))
    if verbose:
        _print_verbose(report)


def _run_ingest(
    brain_root: Path,
    *,
    source: str,
    dry_run: bool,
    limit: int | None,
    auto_reindex: bool | None,
) -> Any:
    from brain.pipeline.ingest import ingest

    return ingest(
        brain_root,
        source=source,
        dry_run=dry_run,
        limit=limit,
        auto_reindex=auto_reindex,
    )


def _summary(report: Any) -> str:
    return (
        "Ingest summary: "
        f"processed={report.processed} "
        f"facts_added={report.facts_added} "
        f"review_items_created={report.review_items_created} "
        f"laundry_archived={report.laundry_archived} "
        f"dry_run={str(report.dry_run).lower()}"
    )


def _print_verbose(report: Any) -> None:
    review_files = [str(path) for path in report.review_files]
    if review_files:
        typer.echo("Review files:")
        for path in review_files:
            typer.echo(f"- {path}")

    if report.errors:
        typer.echo("Errors:")
        for error in report.errors:
            typer.echo(f"- {error}")
