from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError

scratch_app = typer.Typer(
    add_completion=False,
    help="Local-only deterministic scratch commands. No external provider is called.",
)


@scratch_app.command("append")
def append_command(
    text: Annotated[
        str | None,
        typer.Argument(help="Short scratch text to append when --stdin is not used."),
    ] = None,
    stdin: Annotated[
        bool,
        typer.Option(
            "--stdin",
            help="Read scratch text from standard input. Local-only deterministic; no provider call.",
        ),
    ] = False,
    source: Annotated[
        str,
        typer.Option("--source", help="Source label recorded with the scratch entry."),
    ] = "manual",
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
) -> None:
    """Append working scratch text locally and deterministically without provider use."""
    try:
        content = _read_input(text=text, stdin=stdin)
        report = _run_append(_root(brain_root), content, source=source)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary(report, source=source, text=content))


def _read_input(*, text: str | None, stdin: bool) -> str:
    if stdin and text is not None:
        raise BrainError("Choose only one scratch input source")
    if stdin:
        content = typer.get_text_stream("stdin").read()
    elif text is not None:
        content = text
    else:
        raise BrainError("Scratch input is required; pass --stdin or text")

    content = content.strip()
    if not content:
        raise BrainError("Scratch input is empty")
    return content


def _root(brain_root: Path | None) -> Path:
    return Path.cwd() if brain_root is None else brain_root


def _run_append(brain_root: Path, text: str, *, source: str) -> Any:
    from brain.pipeline.scratch import append_working

    return append_working(brain_root, text, source=source)


def _summary(report: Any, *, source: str, text: str) -> str:
    path = _value(report, "path", default=_value(report, "working_path", default="working"))
    return (
        "Scratch append summary: "
        f"path={path} "
        f"source={_value(report, 'source', default=source)} "
        f"chars={_value(report, 'chars', default=len(text))}"
    )


def _value(item: Any, name: str, *, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)
