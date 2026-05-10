from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import ulid
from pydantic import BaseModel, ConfigDict

from brain.config import load_config
from brain.exceptions import BrainError
from brain.ledger import append_event
from brain.models import Event, EventKind, Frontmatter, Page, PageType, ProcedureStatus
from brain.models.page import SLUG_PATTERN
from brain.pages import TimelineEntry, format_entry, parse_page, regenerate_index, write_page
from brain.paths import BrainPaths


class ProcedureRunResult(StrEnum):
    """Supported manual procedure run outcomes."""

    SUCCESS = "success"
    FAIL = "fail"


class ProcedureReport(BaseModel):
    """Summary of a procedure CLI pipeline operation."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    path: str
    status: ProcedureStatus
    success_count: int
    fail_count: int
    last_run: datetime | None = None
    event_id: str | None = None
    summary: str


def create_procedure(
    brain_root: Path,
    slug: str,
    *,
    title: str,
    auto_commit: bool | None = None,
) -> ProcedureReport:
    """Create a manual procedure page."""
    _validate_slug(slug)
    clean_title = title.strip()
    if not clean_title:
        raise BrainError("procedure title is required")

    paths = BrainPaths(Path(brain_root))
    path = paths.procedures_dir / f"{slug}.md"
    if path.exists():
        raise BrainError(f"Procedure already exists: {slug}")
    _ensure_slug_available(paths, slug)

    now = _now_utc()
    page = Page(
        frontmatter=Frontmatter(
            type=PageType.PROCEDURE,
            slug=slug,
            title=clean_title,
            created=now,
            updated=now,
            tags=[],
            aliases=[],
            external_ids={},
            status=ProcedureStatus.RAW,
            success_count=0,
            fail_count=0,
        ),
        compiled_truth=f"Procedure SOP placeholder for {clean_title}.",
        timeline=[],
        sources=[],
    )
    write_page(path, page)
    _regenerate_index(paths)
    _auto_commit(paths, auto_commit, f"procedure: create {slug}", [path, paths.pages_index])

    return ProcedureReport(
        slug=slug,
        path=_relative_path(path, paths.root),
        status=ProcedureStatus.RAW,
        success_count=0,
        fail_count=0,
        summary=f"Created procedure {slug}",
    )


def run_procedure(
    brain_root: Path,
    slug: str,
    *,
    result: str | ProcedureRunResult,
    note: str,
    auto_commit: bool | None = None,
) -> ProcedureReport:
    """Record one manual procedure run and append a canonical timeline entry."""
    _validate_slug(slug)
    outcome = _coerce_run_result(result)
    clean_note = _single_line(note)
    if not clean_note:
        raise BrainError("procedure run note is required")

    paths = BrainPaths(Path(brain_root))
    path = _procedure_path(paths, slug)
    page = _load_procedure_page(path)
    now = _now_utc()
    event = _procedure_event(
        slug,
        kind="run",
        note=clean_note,
        metadata={"result": outcome.value},
        timestamp=now,
    )
    current_status = _procedure_status(page.frontmatter.status)
    next_status = (
        ProcedureStatus.TESTED
        if outcome is ProcedureRunResult.SUCCESS and current_status is ProcedureStatus.RAW
        else current_status
    )
    success_count = _count(page.frontmatter.success_count)
    fail_count = _count(page.frontmatter.fail_count)
    if outcome is ProcedureRunResult.SUCCESS:
        success_count += 1
    else:
        fail_count += 1

    updated_page = page.model_copy(
        update={
            "frontmatter": page.frontmatter.model_copy(
                update={
                    "updated": now,
                    "status": next_status,
                    "success_count": success_count,
                    "fail_count": fail_count,
                    "last_run": now,
                }
            ),
            "timeline": [
                *page.timeline,
                _format_timeline(
                    now,
                    event.id,
                    f"Procedure run {outcome.value}: {clean_note}",
                ),
            ],
        }
    )
    append_event(paths.events_jsonl, event)
    write_page(path, updated_page)
    _regenerate_index(paths)
    _auto_commit(
        paths,
        auto_commit,
        f"procedure: record {outcome.value} for {slug}",
        [path, paths.events_jsonl, paths.pages_index],
    )

    return ProcedureReport(
        slug=slug,
        path=_relative_path(path, paths.root),
        status=next_status,
        success_count=success_count,
        fail_count=fail_count,
        last_run=now,
        event_id=event.id,
        summary=f"Recorded {outcome.value} run for procedure {slug}",
    )


def promote_procedure(
    brain_root: Path,
    slug: str,
    *,
    status: str | ProcedureStatus,
    auto_commit: bool | None = None,
) -> ProcedureReport:
    """Set a procedure lifecycle status and append a canonical timeline entry."""
    _validate_slug(slug)
    next_status = _coerce_status(status)

    paths = BrainPaths(Path(brain_root))
    path = _procedure_path(paths, slug)
    page = _load_procedure_page(path)
    now = _now_utc()
    event = _procedure_event(
        slug,
        kind="promote",
        note=f"status={next_status.value}",
        metadata={"status": next_status.value},
        timestamp=now,
    )

    updated_page = page.model_copy(
        update={
            "frontmatter": page.frontmatter.model_copy(
                update={"updated": now, "status": next_status}
            ),
            "timeline": [
                *page.timeline,
                _format_timeline(
                    now,
                    event.id,
                    f"Procedure status set to {next_status.value}.",
                ),
            ],
        }
    )
    append_event(paths.events_jsonl, event)
    write_page(path, updated_page)
    _regenerate_index(paths)
    _auto_commit(
        paths,
        auto_commit,
        f"procedure: promote {slug} to {next_status.value}",
        [path, paths.events_jsonl, paths.pages_index],
    )

    return ProcedureReport(
        slug=slug,
        path=_relative_path(path, paths.root),
        status=next_status,
        success_count=_count(page.frontmatter.success_count),
        fail_count=_count(page.frontmatter.fail_count),
        last_run=page.frontmatter.last_run,
        event_id=event.id,
        summary=f"Set procedure {slug} status to {next_status.value}",
    )


def _load_procedure_page(path: Path) -> Page:
    page = parse_page(path)
    if page.frontmatter.type is not PageType.PROCEDURE:
        raise BrainError(f"Page is not a procedure: {path}")
    return page


def _procedure_path(paths: BrainPaths, slug: str) -> Path:
    path = paths.procedures_dir / f"{slug}.md"
    if not path.exists():
        raise BrainError(f"Unknown procedure: {slug}")
    return path


def _procedure_event(
    slug: str,
    *,
    kind: str,
    note: str,
    metadata: dict[str, str],
    timestamp: datetime,
) -> Event:
    return Event(
        id=str(ulid.ULID()),
        timestamp=timestamp,
        kind=EventKind.PAGE_EDITED,
        source_ref=f"procedure/{slug}:{kind}",
        raw_payload=note,
        affected_pages=[slug],
        metadata={"procedure": slug, "operation": kind, **metadata},
    )


def _format_timeline(timestamp: datetime, event_id: str, description: str) -> str:
    return format_entry(
        TimelineEntry(
            date=timestamp.date().isoformat(),
            event_id=event_id,
            description=description,
        )
    )


def _regenerate_index(paths: BrainPaths) -> None:
    regenerate_index(paths.root)


def _auto_commit(
    paths: BrainPaths,
    auto_commit: bool | None,
    message: str,
    commit_paths: list[Path],
) -> None:
    config = load_config(paths.config_path)
    should_commit = config.git.auto_commit if auto_commit is None else auto_commit
    if not should_commit:
        return

    from brain import git_ops

    git_ops.commit(paths.root, message, paths=[path for path in commit_paths if path.exists()])


def _ensure_slug_available(paths: BrainPaths, slug: str) -> None:
    if not paths.pages_dir.exists():
        return
    for page_path in paths.pages_dir.rglob("*.md"):
        if page_path in {paths.pages_index, paths.pages_log}:
            continue
        try:
            page = parse_page(page_path)
        except BrainError:
            continue
        if page.frontmatter.slug == slug:
            raise BrainError(f"Page slug already exists: {slug}")


def _validate_slug(slug: str) -> None:
    if not SLUG_PATTERN.fullmatch(slug):
        raise BrainError("slug must be lowercase ASCII words separated by hyphens")


def _coerce_run_result(result: str | ProcedureRunResult) -> ProcedureRunResult:
    try:
        return result if isinstance(result, ProcedureRunResult) else ProcedureRunResult(result)
    except ValueError as exc:
        raise BrainError("procedure result must be success or fail") from exc


def _coerce_status(status: str | ProcedureStatus) -> ProcedureStatus:
    try:
        return status if isinstance(status, ProcedureStatus) else ProcedureStatus(status)
    except ValueError as exc:
        raise BrainError("procedure status must be raw, tested, or stable") from exc


def _procedure_status(status: ProcedureStatus | None) -> ProcedureStatus:
    if status is None:
        raise BrainError("procedure page is missing status")
    return status


def _count(value: int | None) -> int:
    return 0 if value is None else value


def _single_line(value: str) -> str:
    normalized = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()
    if not normalized:
        return ""
    return " ".join(normalized.split())


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
