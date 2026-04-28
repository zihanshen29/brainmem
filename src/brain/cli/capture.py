from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError
from brain.pipeline.capture import VALID_KINDS, capture


def capture_command(
    kind: Annotated[
        str,
        typer.Argument(
            help="Capture kind.",
        ),
    ] = "note",
    stdin: Annotated[
        bool,
        typer.Option(
            "--stdin",
            help="Read capture content from standard input.",
        ),
    ] = False,
    file_path: Annotated[
        Path | None,
        typer.Option(
            "--file",
            exists=False,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Read capture content from a file.",
        ),
    ] = None,
) -> None:
    """Capture raw text into laundry for later ingest."""
    try:
        if kind not in VALID_KINDS:
            raise BrainError(f"Unsupported capture kind: {kind}")
        source_count = int(stdin) + int(file_path is not None)
        if source_count > 1:
            raise BrainError("Choose only one capture source")

        if stdin:
            report = _run_capture(Path.cwd(), typer.get_text_stream("stdin").read(), kind, "stdin")
        elif file_path is not None:
            text = _read_file(file_path)
            report = _run_capture(Path.cwd(), text, kind, "file", str(file_path))
        else:
            text = _read_editor()
            report = _run_capture(Path.cwd(), text, kind, "editor")
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(_summary(report))


def _run_capture(
    brain_root: Path,
    text: str,
    kind: str,
    source: str,
    source_ref: str | None = None,
) -> Any:
    return capture(
        brain_root,
        text,
        kind=kind,
        source=source,
        source_ref=source_ref,
    )


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BrainError(f"Capture file not found: {path}") from exc
    except OSError as exc:
        raise BrainError(f"Could not read capture file: {path}") from exc


def _read_editor() -> str:
    editor = os.environ.get("EDITOR")
    if not editor:
        raise BrainError("EDITOR is not set")

    with tempfile.NamedTemporaryFile(
        mode="w+",
        suffix=".md",
        encoding="utf-8",
        newline="\n",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)

    try:
        result = subprocess.run([*_editor_command(editor), str(temp_path)], check=False)
        if result.returncode != 0:
            raise BrainError(f"Editor failed with exit code {result.returncode}")
        return temp_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BrainError(f"Editor failed: {exc}") from exc
    finally:
        with suppress(FileNotFoundError):
            temp_path.unlink()


def _editor_command(editor: str) -> list[str]:
    try:
        command = shlex.split(editor, posix=os.name != "nt")
    except ValueError as exc:
        raise BrainError(f"Invalid EDITOR command: {editor}") from exc

    if os.name == "nt":
        command = [_strip_outer_quotes(part) for part in command]
    if not command:
        raise BrainError("EDITOR is not set")
    return command


def _strip_outer_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _summary(report: Any) -> str:
    return (
        "Capture summary: "
        f"path={report.path} "
        f"kind={report.kind} "
        f"committed={str(report.committed).lower()}"
    )
