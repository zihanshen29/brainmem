from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError


def reindex_command(
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-embed chunks even when their content hash is unchanged."),
    ] = False,
    pages: Annotated[
        list[str] | None,
        typer.Option("--pages", help="Only reindex the given page slug. May be repeated."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview reindex work without embedding or writing changes."),
    ] = False,
    commit: Annotated[
        bool,
        typer.Option("--commit/--no-commit", help="Commit reindex changes to git."),
    ] = False,
) -> None:
    """Rebuild the embedding index for brain pages."""
    try:
        root = Path.cwd() if brain_root is None else brain_root
        report = _run_reindex(
            root,
            force=force,
            page_filter=pages,
            dry_run=dry_run,
            no_commit=not commit,
        )
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary(report))
    if report.errors:
        typer.echo("Errors:")
        for error in report.errors:
            typer.echo(f"- {error}")


def _run_reindex(
    brain_root: Path,
    *,
    force: bool,
    page_filter: list[str] | None,
    dry_run: bool,
    no_commit: bool,
) -> Any:
    from brain.pipeline.reindex import reindex

    return reindex(
        brain_root,
        force=force,
        page_filter=page_filter,
        dry_run=dry_run,
        no_commit=no_commit,
    )


def _summary(report: Any) -> str:
    return (
        "Reindex summary: "
        f"pages={report.pages_scanned} "
        f"added={report.chunks_added} "
        f"updated={report.chunks_updated} "
        f"removed={report.chunks_removed} "
        f"unchanged={report.chunks_unchanged} "
        f"tokens={report.tokens_used} "
        f"errors={len(report.errors)} "
        f"dry_run={str(report.dry_run).lower()} "
        f"committed={str(report.committed).lower()}"
    )
