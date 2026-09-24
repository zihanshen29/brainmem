from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError
from brain.paths import resolve_brain_root


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


@entity_app.command("literalize")
def literalize_command(
    entity_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Path or file entities to turn into literal fact values."),
    ] = None,
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    predicate: Annotated[
        list[str] | None,
        typer.Option("--predicate", help="Correct a referencing fact: FACT_ID=snake_case_predicate."),
    ] = None,
    fold_into: Annotated[
        str | None,
        typer.Option("--fold-into", help="Project that takes over facts and stub pages of the listed entities."),
    ] = None,
    dangling: Annotated[
        bool,
        typer.Option("--dangling", help="Also turn references to entities that no longer exist into values."),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write the dry-run plan to a file outside the data root."),
    ] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Apply a reviewed plan.")] = False,
    plan: Annotated[Path | None, typer.Option("--plan", help="Reviewed plan file.")] = None,
    backup: Annotated[Path | None, typer.Option("--backup", help="Current verified backup.")] = None,
) -> None:
    """Dry-run by default. Apply requires the reviewed plan and a current verified backup."""
    from brain.pipeline.entity_literalize import apply_literalize, plan_literalize

    root = _root(brain_root)
    try:
        if apply:
            if plan is None or backup is None or entity_ids or predicate or fold_into or dangling:
                raise typer.BadParameter("--apply takes only --plan and --backup")
            result = apply_literalize(root, json.loads(plan.read_text(encoding="utf-8")), backup)
        else:
            result = plan_literalize(
                root,
                entity_ids or [],
                _predicate_corrections(predicate or []),
                fold_into=fold_into,
                dangling=dangling,
            )
            if output is not None:
                if output.resolve().is_relative_to(root.resolve()):
                    raise typer.BadParameter("Dry-run output must be outside the data root")
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result = result["counts"]
    except (BrainError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


def _predicate_corrections(values: list[str]) -> dict[int, str]:
    corrections: dict[int, str] = {}
    for value in values:
        fact_id, separator, name = value.partition("=")
        if not separator or not fact_id.strip().isdigit():
            raise typer.BadParameter(f"Expected FACT_ID=predicate, got {value!r}")
        corrections[int(fact_id)] = name.strip()
    return corrections


def _root(brain_root: Path | None) -> Path:
    return resolve_brain_root(brain_root)


def _run_merge(root: Path, *, slug_a: str, slug_b: str, into: str) -> Any:
    from brain.pipeline.entity_merge import merge_entities
    if into not in {"a", "b"}:
        raise BrainError("into must be a or b")
    from typing import Literal, cast
    return merge_entities(root, slug_a, slug_b, into=cast(Literal["a", "b"], into))


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
        f"embeddings_deleted={_value(report, 'embeddings_deleted', default=0)} "
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
