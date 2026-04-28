from pathlib import Path
from typing import Annotated

import typer

from brain import __version__
from brain.cli.ask import ask_command
from brain.cli.capture import capture_command
from brain.cli.entity import entity_app
from brain.cli.ingest import ingest_command
from brain.cli.init import init_brain
from brain.cli.lint import lint_command
from brain.cli.promote_chat import promote_chat_command
from brain.cli.rebuild import rebuild_command
from brain.cli.review import review_command
from brain.cli.status import status_command
from brain.exceptions import BrainError

app = typer.Typer(add_completion=False, help="Personal memory system CLI.")
app.command("ask")(ask_command)
app.command("capture")(capture_command)
app.command("ingest")(ingest_command)
app.command("lint")(lint_command)
app.command("promote-chat")(promote_chat_command)
app.command("rebuild")(rebuild_command)
app.command("review")(review_command)
app.command("status")(status_command)
app.add_typer(entity_app, name="entity")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version_requested: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            help="Show the installed brain version.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Run the brain command-line interface."""


@app.command()
def version() -> None:
    """Show the installed brain version."""
    typer.echo(__version__)


@app.command("init")
def init_command(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Brain repository root to initialize.",
        ),
    ] = Path("~/brain"),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Remove existing root contents before initialization.",
        ),
    ] = False,
) -> None:
    """Initialize an empty brain repository."""
    try:
        init_brain(root, force=force)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Initialized brain repository at {Path(root).expanduser().resolve()}")
