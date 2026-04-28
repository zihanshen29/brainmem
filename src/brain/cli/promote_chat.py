from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError


def promote_chat_command(
    event_id: Annotated[
        str,
        typer.Argument(
            help="AI chat event id or unambiguous prefix to promote.",
        ),
    ],
    title: Annotated[
        str | None,
        typer.Option(
            "--title",
            help="Override the conversation page title.",
        ),
    ] = None,
    slug: Annotated[
        str | None,
        typer.Option(
            "--slug",
            help="Override the conversation page slug.",
        ),
    ] = None,
) -> None:
    """Promote one AI chat ledger event into a conversation page."""
    try:
        report = _run_promote_chat(Path.cwd(), event_id, title=title, slug=slug)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary(report))


def _run_promote_chat(
    brain_root: Path,
    event_id: str,
    *,
    title: str | None,
    slug: str | None,
) -> Any:
    from brain.pipeline.promote_chat import promote_chat

    return promote_chat(brain_root, event_id, title=title, slug=slug)


def _summary(report: Any) -> str:
    ingest_report = report.ingest_report
    return (
        "Promote chat summary: "
        f"slug={report.page_slug} "
        f"path={report.page_path} "
        f"source_event_id={report.source_event_id} "
        f"page_event_id={report.page_event_id} "
        f"ingest_processed={ingest_report.processed} "
        f"ingest_facts_added={ingest_report.facts_added} "
        f"ingest_review_items_created={ingest_report.review_items_created} "
        f"committed={str(report.committed).lower()}"
    )
