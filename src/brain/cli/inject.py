from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from brain.exceptions import BrainError
from brain.pipeline.injection import OutputFormat

DEFAULT_BUDGET = 10000
DEFAULT_TOP = 8


def inject_command(
    query: Annotated[
        str,
        typer.Option("--query", help="Question or search terms used to select memory fragments."),
    ],
    budget: Annotated[
        int,
        typer.Option("--budget", min=1, help="Maximum estimated tokens for rendered output."),
    ] = DEFAULT_BUDGET,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format: markdown or text."),
    ] = "markdown",
    mode: Annotated[
        str,
        typer.Option("--mode", help="Retrieval mode: hybrid, keyword-only, semantic, or sql."),
    ] = "keyword-only",
    top: Annotated[
        int,
        typer.Option("--top", min=1, help="Maximum number of pages to retrieve before budgeting."),
    ] = DEFAULT_TOP,
    include_snapshot: Annotated[
        bool,
        typer.Option("--snapshot/--no-snapshot", help="Include scratch/SNAPSHOT.md before retrieved pages."),
    ] = True,
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
) -> None:
    """Render token-aware context for prompt injection."""
    try:
        result = _run_inject(
            Path.cwd() if brain_root is None else brain_root,
            query=query,
            budget=budget,
            output_format=output_format,
            mode=mode,
            top=top,
            include_snapshot=include_snapshot,
        )
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    for warning in result.warnings:
        typer.echo(f"Warning: {warning}", err=True)
    typer.echo(result.content, nl=False)


def _run_inject(
    root: Path,
    *,
    query: str,
    budget: int,
    output_format: OutputFormat,
    mode: str,
    top: int,
    include_snapshot: bool,
):
    from brain.pipeline.injection import inject

    return inject(
        root,
        query=query,
        budget=budget,
        output_format=output_format,
        mode=mode,
        top=top,
        include_snapshot=include_snapshot,
    )
