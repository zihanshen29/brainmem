from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError

DEFAULT_MAX_ITEMS = 20
DEFAULT_MAX_CHARS = 8000
DEFAULT_STRATEGY = "dedup"

snapshot_app = typer.Typer(
    add_completion=False,
    help="Local-only deterministic snapshot commands. No external provider is called.",
)


@snapshot_app.command("rebuild")
def rebuild_command(
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    max_items: Annotated[
        int,
        typer.Option(
            "--max-items",
            min=1,
            help="Maximum scratch items to include. Local-only deterministic; no provider call.",
        ),
    ] = DEFAULT_MAX_ITEMS,
    max_chars: Annotated[
        int,
        typer.Option(
            "--max-chars",
            min=1,
            help="Maximum snapshot characters. Local-only deterministic; no provider call.",
        ),
    ] = DEFAULT_MAX_CHARS,
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            help="Snapshot strategy: dedup keeps the latest item per source; recent keeps newest items.",
        ),
    ] = DEFAULT_STRATEGY,
) -> None:
    """Rebuild SNAPSHOT locally and deterministically without provider use."""
    try:
        report = _run_rebuild(
            _root(brain_root),
            max_items=max_items,
            max_chars=max_chars,
            strategy=strategy,
        )
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary(report))


def _root(brain_root: Path | None) -> Path:
    return Path.cwd() if brain_root is None else brain_root


def _run_rebuild(
    brain_root: Path,
    *,
    max_items: int,
    max_chars: int,
    strategy: str,
) -> Any:
    from brain.pipeline.scratch import rebuild_snapshot

    return rebuild_snapshot(
        brain_root,
        max_items=max_items,
        max_chars=max_chars,
        strategy=strategy,
    )


def _summary(report: Any) -> str:
    path = _value(report, "path", default=_value(report, "snapshot_path", default="SNAPSHOT"))
    return (
        "Snapshot rebuild summary: "
        f"path={path} "
        f"strategy={_value(report, 'strategy', default='dedup')} "
        f"items={_value(report, 'entries', default=0)} "
        f"chars={_value(report, 'char_count', default=0)}"
    )


def _value(item: Any, name: str, *, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)
