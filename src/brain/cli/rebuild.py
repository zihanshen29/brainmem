from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError


def rebuild_command(
    db: Annotated[
        bool,
        typer.Option("--db", help="Rebuild brain.db, backlinks, and pages/index.md."),
    ] = False,
    pages: Annotated[
        str | None,
        typer.Option("--pages", help="Rewrite compiled truth for one page slug."),
    ] = None,
    backlinks: Annotated[
        bool,
        typer.Option("--backlinks", help="Rebuild backlink rows from current markdown."),
    ] = False,
    index: Annotated[
        bool,
        typer.Option("--index", help="Regenerate pages/index.md."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Required with --pages to rewrite compiled truth."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip confirmation prompts."),
    ] = False,
) -> None:
    """Rebuild derived brain artifacts in the current repository."""
    selected = _selected_scopes(db=db, pages=pages, backlinks=backlinks, index=index)
    if len(selected) != 1:
        typer.echo("Error: select exactly one rebuild scope", err=True)
        raise typer.Exit(1)

    scope = selected[0]
    if scope == "pages" and not force:
        typer.echo("Error: --pages requires --force", err=True)
        raise typer.Exit(1)

    if _requires_confirmation(scope) and not yes and not _confirm(scope, pages):
        typer.echo("Error: rebuild cancelled", err=True)
        raise typer.Exit(1)

    try:
        report = _run_rebuild(Path.cwd(), scope=scope, pages=pages, force=force)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary(report))


def _selected_scopes(
    *,
    db: bool,
    pages: str | None,
    backlinks: bool,
    index: bool,
) -> list[str]:
    selected: list[str] = []
    if db:
        selected.append("db")
    if pages is not None:
        selected.append("pages")
    if backlinks:
        selected.append("backlinks")
    if index:
        selected.append("index")
    return selected


def _requires_confirmation(scope: str) -> bool:
    return scope in {"db", "pages", "backlinks"}


def _confirm(scope: str, pages: str | None) -> bool:
    target = f"page {pages!r}" if scope == "pages" else scope
    return typer.confirm(f"Rebuild {target}?", default=False, abort=False)


def _run_rebuild(root: Path, *, scope: str, pages: str | None, force: bool) -> Any:
    from brain.pipeline import rebuild_backlinks, rebuild_db, rebuild_index, rebuild_pages

    if scope == "db":
        return rebuild_db(root)
    if scope == "pages":
        if pages is None:
            raise BrainError("--pages requires a slug")
        return rebuild_pages(root, pages, force=force)
    if scope == "backlinks":
        return rebuild_backlinks(root)
    if scope == "index":
        return rebuild_index(root)
    raise BrainError(f"unknown rebuild scope: {scope}")


def _summary(report: Any) -> str:
    return (
        "Rebuild summary: "
        f"scope={_value(report, 'scope')} "
        f"pages_scanned={_value(report, 'pages_scanned', default=0)} "
        f"pages_touched={len(_list_value(report, 'pages_touched'))} "
        f"entities_rebuilt={_value(report, 'entities_rebuilt', default=0)} "
        f"aliases_rebuilt={_value(report, 'aliases_rebuilt', default=0)} "
        f"backlinks_rebuilt={_value(report, 'backlinks_rebuilt', default=0)} "
        f"index_rebuilt={_bool_text(_value(report, 'index_rebuilt', default=False))} "
        f"facts_rebuilt={_value(report, 'facts_rebuilt', default=0)} "
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
