from typing import Annotated

import typer

from brain import __version__

app = typer.Typer(add_completion=False, help="Personal memory system CLI.")


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
