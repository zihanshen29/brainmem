from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError

LINT_KINDS = ("contradictions", "stale", "orphans", "citations")


def lint_command(
    all_: Annotated[
        bool,
        typer.Option("--all", help="Run all lint checks."),
    ] = False,
    contradictions: Annotated[
        bool,
        typer.Option("--contradictions", help="Find conflicting active facts."),
    ] = False,
    stale: Annotated[
        bool,
        typer.Option("--stale", help="Find stale tier 1 pages."),
    ] = False,
    days: Annotated[
        int | None,
        typer.Option("--days", min=1, help="Override stale page threshold in days."),
    ] = None,
    orphans: Annotated[
        bool,
        typer.Option("--orphans", help="Find timeline wikilinks with missing entity pages."),
    ] = False,
    citations: Annotated[
        bool,
        typer.Option("--citations", help="Find compiled-truth wikilinks missing from timeline."),
    ] = False,
) -> None:
    """Run deterministic lint checks against the current brain repository."""
    kinds = _selected_kinds(
        all_=all_,
        contradictions=contradictions,
        stale=stale,
        orphans=orphans,
        citations=citations,
    )
    if not kinds:
        typer.echo("Error: select at least one lint check or pass --all", err=True)
        raise typer.Exit(1)

    if days is not None and "stale" not in kinds:
        typer.echo("Error: --days can only be used with --stale or --all", err=True)
        raise typer.Exit(1)

    try:
        report = _run_lint(Path.cwd(), kinds, stale_days=days)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    counts = _issue_counts(report, kinds)
    review_files = _review_files(report)
    total = _total_issues(report, counts)

    typer.echo("Lint summary:")
    for kind in kinds:
        typer.echo(f"- {kind}: {counts.get(kind, 0)} issues")

    if review_files:
        typer.echo("Review files:")
        for path in review_files:
            typer.echo(f"- {path}")

    typer.echo(f"Total issues: {total}")


def _run_lint(root: Path, kinds: list[str], *, stale_days: int | None = None) -> Any:
    try:
        from brain.pipeline import run_lint
    except ImportError:
        try:
            from brain.pipeline.lint import run_lint
        except ImportError as exc:
            raise BrainError("lint pipeline is not available") from exc

    return run_lint(root, kinds, stale_days=stale_days)


def _selected_kinds(
    *,
    all_: bool,
    contradictions: bool,
    stale: bool,
    orphans: bool,
    citations: bool,
) -> list[str]:
    if all_:
        return list(LINT_KINDS)

    selected: list[str] = []
    if contradictions:
        selected.append("contradictions")
    if stale:
        selected.append("stale")
    if orphans:
        selected.append("orphans")
    if citations:
        selected.append("citations")
    return selected


def _issue_counts(report: Any, kinds: list[str]) -> dict[str, int]:
    raw_counts = _value(report, "issue_counts", "counts", "kind_counts", "issues_by_kind")
    counts = _counts_from_mapping(raw_counts)
    if counts:
        return {kind: counts.get(kind, 0) for kind in kinds}

    results = _list_value(report, "results", "items", "findings")
    counts = {kind: 0 for kind in kinds}
    for item in results:
        kind = _enum_value(_value(item, "kind"))
        if kind not in counts:
            continue
        count = _value(item, "issue_count", "issues", "count")
        counts[kind] += len(count) if isinstance(count, list) else int(count or 0)
    if any(counts.values()):
        return counts

    if len(kinds) == 1:
        total = _value(report, "issue_count", "total_issues", "total")
        if total is not None:
            counts[kinds[0]] = int(total)
    return counts


def _counts_from_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}

    counts: dict[str, int] = {}
    for key, count in value.items():
        kind = str(_enum_value(key))
        counts[kind] = len(count) if isinstance(count, list) else int(count or 0)
    return counts


def _review_files(report: Any) -> list[str]:
    files = _list_value(report, "review_files", "report_files")
    if files:
        return [str(path) for path in files]

    results = _list_value(report, "results", "items", "findings")
    found = []
    for item in results:
        path = _value(item, "review_file", "report_file", "path")
        if path is not None:
            found.append(str(path))
    return found


def _total_issues(report: Any, counts: dict[str, int]) -> int:
    total = _value(report, "total_issues", "total", "issue_count")
    if total is not None:
        return int(total)
    return sum(counts.values())


def _list_value(item: Any, *names: str) -> list[Any]:
    value = _value(item, *names)
    if value is None:
        return []
    return list(value)


def _value(item: Any, *names: str) -> Any:
    for name in names:
        if isinstance(item, dict):
            if name in item:
                return item[name]
            continue
        if hasattr(item, name):
            return getattr(item, name)
    return None


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)
