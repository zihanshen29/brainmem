from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError


def review_command(
    review_id: Annotated[
        str | None,
        typer.Argument(help="Review id or unique prefix to open."),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="Filter pending review items by kind."),
    ] = None,
    apply_: Annotated[
        bool,
        typer.Option("--apply", help="Apply checked pending review decisions."),
    ] = False,
) -> None:
    """List, open, or apply pending review decisions."""
    root = Path.cwd()
    try:
        if apply_:
            if review_id is not None:
                raise BrainError("Cannot combine a review id with --apply")
            report = apply_pending(root, kind=kind)
            typer.echo(_format_apply_report(report))
            for follow_up in _list_value(report, "follow_ups"):
                typer.echo(f"- follow-up: {follow_up}")
            for error in _list_value(report, "errors"):
                typer.echo(f"- {error}", err=True)
            return

        if review_id is not None:
            path = resolve_review_path(root, review_id)
            _open_editor(path)
            decision = _decision_value(parse_review_file(path))
            if decision is None:
                typer.echo(f"Opened {path}; no decision selected.")
            else:
                typer.echo(f"Opened {path}; decision={decision}.")
            return

        items = list_pending(root, kind=kind)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if not items:
        typer.echo("No pending review items.")
        return

    typer.echo("Pending review items:")
    for item in items:
        typer.echo(_format_pending_item(root, item))


def _open_editor(path: Path) -> None:
    editor = os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if os.name == "nt" else "vi"
    command = shlex.split(editor, posix=os.name != "nt")
    if not command:
        command = [editor]
    try:
        subprocess.run([*command, str(path)], check=True)
    except OSError as exc:
        raise BrainError(f"could not start editor {command[0]!r}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise BrainError(f"editor exited with status {exc.returncode}") from exc


def list_pending(root: Path, *, kind: str | None = None) -> list[Any]:
    try:
        from brain.pipeline.review import list_pending as pipeline_list_pending
    except ImportError as exc:
        raise BrainError("review pipeline is not available") from exc

    return list(pipeline_list_pending(root, kind=kind))


def resolve_review_path(root: Path, review_id: str) -> Path:
    try:
        from brain.pipeline.review import resolve_review_path as pipeline_resolve_review_path
    except ImportError as exc:
        raise BrainError("review pipeline is not available") from exc

    return Path(pipeline_resolve_review_path(root, review_id))


def parse_review_file(path: Path) -> Any:
    try:
        from brain.pipeline.review import parse_review_file as pipeline_parse_review_file
    except ImportError as exc:
        raise BrainError("review pipeline is not available") from exc

    return pipeline_parse_review_file(path)


def apply_pending(root: Path, *, kind: str | None = None) -> Any:
    try:
        from brain.pipeline.review import apply_pending as pipeline_apply_pending
    except ImportError as exc:
        raise BrainError("review pipeline is not available") from exc

    return pipeline_apply_pending(root, kind=kind)


def _format_pending_item(root: Path, item: Any) -> str:
    review_id = _value(item, "review_id", "id")
    kind = _enum_value(_value(item, "kind"))
    status = _enum_value(_value(item, "status"))
    path = _value(item, "path", "review_file", "file")

    if review_id is None and path is not None:
        review_id = Path(str(path)).stem
    label = str(review_id) if review_id is not None else str(item)

    details = [str(value) for value in (kind, status) if value is not None]
    suffix = f" [{' | '.join(details)}]" if details else ""
    if path is None:
        return f"- {label}{suffix}"

    return f"- {label}{suffix} {_display_path(root, Path(path))}"


def _format_apply_report(report: Any) -> str:
    fields = [
        ("applied", "applied"),
        ("approved", "approved"),
        ("rejected", "rejected"),
        ("deferred", "deferred"),
        ("archived", "archived"),
        ("skipped", "skipped"),
    ]
    parts = ["Review apply summary:"]
    for attr, label in fields:
        value = _value(report, attr)
        if value is not None:
            parts.append(f"{label}={value}")

    errors = _list_value(report, "errors")
    if errors:
        parts.append(f"errors={len(errors)}")
    follow_ups = _list_value(report, "follow_ups")
    if follow_ups:
        parts.append(f"follow_ups={len(follow_ups)}")
    if len(parts) == 1:
        parts.append(str(report))
    return " ".join(parts)


def _decision_value(parsed: Any) -> str | None:
    decision = _value(parsed, "decision", "action")
    if decision is None:
        return None

    text = str(_enum_value(decision)).strip().lower()
    if not text or text == "none":
        return None
    return text


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _list_value(item: Any, name: str) -> list[Any]:
    value = _value(item, name)
    if value is None:
        return []
    return list(value)


def _value(item: Any, *names: str) -> Any:
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return None


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)
