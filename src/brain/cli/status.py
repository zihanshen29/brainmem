from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from brain.exceptions import BrainError
from brain.pipeline.status import StatusReport, collect_status


def status_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print status as JSON."),
    ] = False,
) -> None:
    """Show read-only brain repository status."""
    try:
        report = collect_status(Path.cwd())
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
            f"Pending reviews: {report.pending_reviews}",
            f"Last ingest: {report.last_ingest_at or 'never'}",
            f"Git dirty: {str(report.git_dirty).lower()}",
        ]
    )


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in counts.items())
