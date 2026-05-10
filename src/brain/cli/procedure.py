from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError
from brain.models import ProcedureStatus
from brain.pipeline.procedure import ProcedureRunResult

procedure_app = typer.Typer(
    add_completion=False,
    help="Manual Procedure Capsules commands. Local-only deterministic; no provider is called.",
)


@procedure_app.command("new")
def new_command(
    slug: Annotated[str, typer.Argument(help="Procedure slug.")],
    title: Annotated[str, typer.Option("--title", help="Procedure title.")],
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
) -> None:
    """Create a manual procedure page."""
    try:
        report = _create(_root(brain_root), slug, title=title)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary("Procedure new summary:", report))


@procedure_app.command("run")
def run_command(
    slug: Annotated[str, typer.Argument(help="Procedure slug.")],
    result: Annotated[
        ProcedureRunResult,
        typer.Option("--result", help="Run result."),
    ],
    note: Annotated[str, typer.Option("--note", help="Run note.")],
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
) -> None:
    """Record a manual procedure run."""
    try:
        report = _run(_root(brain_root), slug, result=result, note=note)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary("Procedure run summary:", report))


@procedure_app.command("promote")
def promote_command(
    slug: Annotated[str, typer.Argument(help="Procedure slug.")],
    status: Annotated[
        ProcedureStatus,
        typer.Option("--status", help="Procedure lifecycle status."),
    ],
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
) -> None:
    """Set a manual procedure lifecycle status."""
    try:
        report = _promote(_root(brain_root), slug, status=status)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary("Procedure promote summary:", report))


def _root(brain_root: Path | None) -> Path:
    return Path.cwd() if brain_root is None else brain_root


def _create(brain_root: Path, slug: str, *, title: str) -> Any:
    from brain.pipeline.procedure import create_procedure

    return create_procedure(brain_root, slug, title=title)


def _run(
    brain_root: Path,
    slug: str,
    *,
    result: ProcedureRunResult,
    note: str,
) -> Any:
    from brain.pipeline.procedure import run_procedure

    return run_procedure(brain_root, slug, result=result, note=note)


def _promote(brain_root: Path, slug: str, *, status: ProcedureStatus) -> Any:
    from brain.pipeline.procedure import promote_procedure

    return promote_procedure(brain_root, slug, status=status)


def _summary(prefix: str, report: Any) -> str:
    parts = [
        prefix,
        f"slug={_value(report, 'slug')}",
        f"path={_value(report, 'path')}",
        f"status={_enum_value(_value(report, 'status'))}",
        f"success_count={_value(report, 'success_count')}",
        f"fail_count={_value(report, 'fail_count')}",
    ]
    last_run = _value(report, "last_run")
    event_id = _value(report, "event_id")
    if last_run is not None:
        parts.append(f"last_run={last_run.isoformat()}")
    if event_id is not None:
        parts.append(f"event_id={event_id}")
    return " ".join(parts)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)
