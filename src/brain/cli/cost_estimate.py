from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast

import typer

from brain.exceptions import BrainError
from brain.import_.cost import cost_estimate
from brain.import_.discovery import discover_files
from brain.models.import_job import CostEstimate, ImportFileKind

DEFAULT_KINDS: set[ImportFileKind] = {"md", "txt", "pdf", "jsonl"}


def cost_estimate_command(
    path: Annotated[
        Path,
        typer.Argument(help="File or directory to estimate for import."),
    ],
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="Comma-separated file kinds: md,txt,pdf,jsonl."),
    ] = None,
) -> None:
    """Estimate local bulk import cost without writing anything."""
    try:
        source = Path(path).expanduser()
        if not source.exists():
            raise BrainError(f"Import path not found: {source}")
        files = discover_files(source, kinds=_parse_kinds(kind))
        estimate = cost_estimate(files)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary(estimate))


def _parse_kinds(value: str | None) -> set[ImportFileKind] | None:
    if value is None:
        return None
    parsed = {part.strip().lower().lstrip(".") for part in value.split(",") if part.strip()}
    unsupported = parsed - DEFAULT_KINDS
    if unsupported:
        raise BrainError(f"Unsupported import kinds: {', '.join(sorted(unsupported))}")
    return cast("set[ImportFileKind]", parsed) or None


def _summary(estimate: CostEstimate) -> str:
    by_kind = estimate.by_kind
    kind_summary = ", ".join(f"{key}={value}" for key, value in sorted(by_kind.items())) or "none"
    return (
        "Cost estimate: "
        f"files={estimate.total_files} "
        f"by_kind={kind_summary} "
        f"estimated_extraction_tokens={estimate.estimated_extraction_tokens} "
        f"estimated_embedding_tokens={estimate.estimated_embedding_tokens} "
        f"estimated_total_usd=${estimate.estimated_total_usd:.4f}"
    )
