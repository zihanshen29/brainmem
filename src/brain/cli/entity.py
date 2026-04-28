from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError


class MergeInto(StrEnum):
    """Canonical side selector for entity merges."""

    A = "a"
    B = "b"


entity_app = typer.Typer(help="Entity maintenance commands.")


@entity_app.callback()
def entity_group() -> None:
    """Run entity maintenance commands."""


@entity_app.command("merge")
def merge_command(
    slug_a: Annotated[str, typer.Argument(help="First entity slug.")],
    slug_b: Annotated[str, typer.Argument(help="Second entity slug.")],
    into: Annotated[
        MergeInto,
        typer.Option("--into", help="Canonical side to keep.", case_sensitive=False),
    ] = MergeInto.A,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip confirmation prompt."),
    ] = False,
) -> None:
    """Merge two entity pages and registry rows."""
    canonical = slug_a if into is MergeInto.A else slug_b
    loser = slug_b if into is MergeInto.A else slug_a
    if not yes:
        typer.echo(_preview(slug_a=slug_a, slug_b=slug_b, canonical=canonical, loser=loser))
        if not typer.confirm("Merge entities?", default=False, abort=False):
            typer.echo("Error: entity merge cancelled", err=True)
            raise typer.Exit(1)

    try:
        report = _run_merge(Path.cwd(), slug_a=slug_a, slug_b=slug_b, into=into.value)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary(report))


def _run_merge(root: Path, *, slug_a: str, slug_b: str, into: str) -> Any:
    from brain.pipeline.entity_merge import merge_entities

    return merge_entities(root, slug_a, slug_b, into=into)


def _preview(*, slug_a: str, slug_b: str, canonical: str, loser: str) -> str:
    return (
        "Entity merge summary: "
        f"a={slug_a} b={slug_b} canonical={canonical} loser={loser}"
    )


def _summary(report: Any) -> str:
    return (
        "Entity merge summary: "
        f"canonical={_value(report, 'canonical')} "
        f"loser={_value(report, 'loser')} "
        f"aliases_added={len(_list_value(report, 'aliases_added'))} "
        f"facts_updated={_value(report, 'facts_updated', default=0)} "
        f"backlinks_rebuilt={_value(report, 'backlinks_rebuilt', default=0)} "
        f"tier_proposals_updated={_value(report, 'tier_proposals_updated', default=0)} "
        f"pages_touched={len(_list_value(report, 'pages_touched'))} "
        f"index_rebuilt={_bool_text(_value(report, 'index_rebuilt', default=False))} "
        f"committed={_bool_text(_value(report, 'committed', default=False))}"
    )


def _list_value(item: Any, name: str) -> list[Any]:
    value = _value(item, name)
    if value is None:
        return []
    return list(value)


def _value(item: Any, name: str, *, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _bool_text(value: Any) -> str:
    return str(bool(value)).lower()
