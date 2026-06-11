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
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
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
        report = _run_merge(_root(brain_root), slug_a=slug_a, slug_b=slug_b, into=into.value)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary(report))


@entity_app.command("prune-stub")
def prune_stub_command(
    slugs: Annotated[list[str], typer.Argument(help="Generated stub entity slugs to remove.")],
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    delete_facts: Annotated[
        bool,
        typer.Option("--delete-facts", help="Delete facts whose subject/object is a pruned slug."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip confirmation prompt."),
    ] = False,
) -> None:
    """Remove mistaken generated stub entity/concept pages and registry rows."""
    if not yes:
        typer.echo(f"Entity prune summary: slugs={', '.join(slugs)} delete_facts={delete_facts}")
        if not typer.confirm("Prune stub entities?", default=False, abort=False):
            typer.echo("Error: entity prune cancelled", err=True)
            raise typer.Exit(1)

    try:
        report = _run_prune_stub(_root(brain_root), slugs=slugs, delete_facts=delete_facts)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_prune_summary(report))


def _root(brain_root: Path | None) -> Path:
    return Path.cwd() if brain_root is None else brain_root


def _run_merge(root: Path, *, slug_a: str, slug_b: str, into: str) -> Any:
    from brain.pipeline.entity_merge import merge_entities

    return merge_entities(root, slug_a, slug_b, into=into)


def _run_prune_stub(root: Path, *, slugs: list[str], delete_facts: bool) -> Any:
    from brain.pipeline.entity_prune import prune_stub_entities

    return prune_stub_entities(root, slugs, delete_facts=delete_facts)


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


def _prune_summary(report: Any) -> str:
    return (
        "Entity prune summary: "
        f"slugs={len(_list_value(report, 'slugs'))} "
        f"pages_removed={len(_list_value(report, 'pages_removed'))} "
        f"facts_deleted={_value(report, 'facts_deleted', default=0)} "
        f"pages_rewritten={len(_list_value(report, 'pages_rewritten'))} "
        f"backlinks_rebuilt={_value(report, 'backlinks_rebuilt', default=0)} "
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
