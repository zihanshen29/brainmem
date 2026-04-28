from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError
from brain.models import PageType
from brain.pages.timeline import parse_entry

DEFAULT_TOP = 5


def ask_command(
    query: Annotated[str, typer.Argument(help="Question or search terms.")],
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Print an LLM answer grounded in returned pages."),
    ] = False,
    show_sql: Annotated[
        bool,
        typer.Option("--sql", help="Print deterministic SQL trace before results."),
    ] = False,
    page_type: Annotated[
        PageType | None,
        typer.Option("--type", help="Limit results to one page type.", case_sensitive=False),
    ] = None,
    top: Annotated[
        int,
        typer.Option("--top", min=1, help="Maximum number of results to return."),
    ] = DEFAULT_TOP,
) -> None:
    """Ask a lexical question over pages in the current brain repository."""
    try:
        result = _run_ask(
            Path.cwd(),
            query=query,
            top=top,
            page_type=page_type,
            explain=explain,
            show_sql=show_sql,
        )
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if show_sql and result.trace is not None:
        _print_sql_trace(result.trace)

    _print_results(result.results)

    if result.answer:
        typer.echo("Answer:")
        typer.echo(result.answer)
        if result.sources:
            typer.echo("")
            typer.echo("Sources:")
            for source in result.sources:
                typer.echo(f"- {source}")


def _run_ask(
    root: Path,
    *,
    query: str,
    top: int,
    page_type: PageType | None,
    explain: bool,
    show_sql: bool,
) -> Any:
    from brain.pipeline.ask import ask

    return ask(
        root,
        query=query,
        top=top,
        page_type=page_type,
        explain=explain,
        show_sql=show_sql,
    )


def _print_sql_trace(trace: Any) -> None:
    typer.echo("SQL trace:")
    for item in trace.sql:
        typer.echo(str(item["sql"]))
        typer.echo(f"Params: {item['params']}")
    if trace.matched_entities:
        typer.echo(f"Matched entities: {', '.join(trace.matched_entities)}")
    if trace.boosted_pages:
        typer.echo(f"Boosted pages: {', '.join(trace.boosted_pages)}")
    typer.echo("")


def _print_results(results: list[Any]) -> None:
    if not results:
        typer.echo("No results.")
        return

    for index, page in enumerate(results, start=1):
        page_type = getattr(page.page_type, "value", page.page_type)
        typer.echo(f"{index}. [{page_type}] {page.slug} - {page.title}")
        typer.echo(f"   Compiled truth: {page.compiled_truth}")
        recent = _recent_text(page.recent_timeline)
        typer.echo(f"   Recent: {recent if recent else 'none'}")
        typer.echo(f"   Score: {page.score:.2f}")
        typer.echo("")


def _recent_text(timeline: list[str]) -> str:
    entries: list[str] = []
    for line in timeline:
        try:
            entry = parse_entry(line)
        except BrainError:
            entries.append(line)
        else:
            entries.append(f"{entry.date}: {entry.description}")
    return "; ".join(entries)
