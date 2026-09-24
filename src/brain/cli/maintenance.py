import json
from pathlib import Path
from typing import Annotated

import typer

from brain.exceptions import BrainError
from brain.paths import resolve_brain_root

RootOption = Annotated[Path | None, typer.Option("--brain-root")]


def _run(function, *args, **kwargs):
    try:
        result = function(*args, **kwargs)
    except (BrainError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


def backup_command(destination: Path, brain_root: RootOption = None):
    """Create and verify a complete local backup outside the brain root."""
    from brain.backup import create_backup

    _run(create_backup, resolve_brain_root(brain_root), destination)


def restore_command(
    archive: Path,
    destination: Annotated[Path | None, typer.Option("--destination")] = None,
    verify: Annotated[bool, typer.Option("--verify")] = False,
):
    """Verify a backup or restore to a new directory; never overwrite a root."""
    from brain.backup import restore_backup, verify_backup

    if verify:
        _run(verify_backup, archive)
    elif destination is not None:
        _run(restore_backup, archive, destination)
    else:
        raise typer.BadParameter("Use --verify or --destination <new-directory>")


def reconcile_command(
    brain_root: RootOption = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    plan: Annotated[Path | None, typer.Option("--plan")] = None,
    backup: Annotated[Path | None, typer.Option("--backup")] = None,
):
    """Dry-run by default. Apply requires the reviewed plan and a current verified backup."""
    from brain.pipeline.reconcile import apply_reconcile, plan_reconcile

    root = resolve_brain_root(brain_root)
    if apply:
        if plan is None or backup is None:
            raise typer.BadParameter("--apply requires --plan and --backup")
        _run(apply_reconcile, root, json.loads(plan.read_text(encoding="utf-8")), backup)
    else:
        result = plan_reconcile(root)
        if output is not None:
            if output.resolve().is_relative_to(root):
                raise typer.BadParameter("Dry-run output must be outside the real data root")
            output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            typer.echo(json.dumps(result["counts"], ensure_ascii=False))
        else:
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


def summarize_command(
    slug: str,
    brain_root: RootOption = None,
    provider: Annotated[bool, typer.Option("--provider")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview a local summary without creating a review or calling a model.")] = False,
    apply: Annotated[bool, typer.Option("--apply", help="Write the local summary now if the page is a stub or unedited generated text; it keeps following accepted facts.")] = False,
):
    """Create a summary review draft. --provider sends allowed evidence to the configured model."""
    from brain.pipeline.summaries import apply_local_summary, propose_summary

    root = resolve_brain_root(brain_root)
    if apply:
        if provider or dry_run:
            raise typer.BadParameter("--apply writes the local summary; use --dry-run to preview and review provider drafts")
        _run(apply_local_summary, root, slug)
    else:
        _run(propose_summary, root, slug, provider=provider, dry_run=dry_run)


def recover_command(brain_root: RootOption = None):
    """Recover an interrupted file/SQLite write without replaying model calls."""
    from brain.concurrency import root_lock

    with root_lock(resolve_brain_root(brain_root), write=True):
        typer.echo("Recovery complete")
