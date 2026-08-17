from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from brain.exceptions import BrainError
from brain.pipeline.status import FileHealth, StatusReport, collect_status


def status_command(
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print status as JSON."),
    ] = False,
) -> None:
    """Show read-only brain repository status."""
    try:
        report = collect_status(Path.cwd() if brain_root is None else brain_root)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if json_output:
        typer.echo(json.dumps(report.to_dict(), sort_keys=True))
    else:
        typer.echo(_human_summary(report))


def _human_summary(report: StatusReport) -> str:
    return "\n".join(
        [
            f"Brain root: {report.brain_root}",
            f"Pages by type: {_format_counts(report.pages_by_type)}",
            f"Entities by tier: {_format_counts(report.entities_by_tier)}",
            (
                "Facts active/superseded: "
                f"{report.facts_active}/{report.facts_superseded}"
            ),
            f"Events count: {report.events_count}",
            (
                "Laundry pending/failed: "
                f"{report.laundry['pending']}/{report.laundry['failed']}"
            ),
            f"Pending reviews: {report.pending_reviews}",
            (
                "Pending reviews by kind: "
                f"{_format_counts(report.pending_reviews_by_kind) or 'none'}"
            ),
            _format_file_health("Scratch working", report.scratch["working"]),
            _format_file_health("Scratch snapshot", report.scratch["snapshot"]),
            f"Last ingest: {report.last_ingest_at or 'never'}",
            f"Git dirty: {str(report.git_dirty).lower()}",
            f"Embedding coverage: {_format_embedding_coverage(report.embedding_coverage)}",
            f"Last reindex: {report.last_reindex_at or 'never'}",
            f"Active import jobs: {report.active_import_jobs}",
            f"Token usage: {_format_counts(report.token_usage)}",
            f"Total cost: ${report.total_cost_usd:.6f}",
        ]
    )


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def _format_embedding_coverage(coverage: dict[str, int | float]) -> str:
    total = int(coverage.get("total_chunks", 0))
    indexed = int(coverage.get("indexed_chunks", 0))
    ratio = float(coverage.get("ratio", 0.0))
    return f"{indexed}/{total} ({ratio:.1%})"


def _format_file_health(label: str, health: FileHealth) -> str:
    if not health.exists:
        return f"{label}: missing"
    return f"{label}: present (updated {health.updated_at or 'unknown'})"
