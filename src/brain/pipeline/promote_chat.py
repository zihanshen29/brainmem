from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import ulid
from pydantic import BaseModel, ConfigDict

from brain.config import load_config
from brain.exceptions import BrainError, PageParseError
from brain.ledger import append_event, read_all
from brain.llm import client as llm_client
from brain.models import Event, EventKind, Frontmatter, Page, PageType
from brain.models.page import SLUG_PATTERN
from brain.pages import TimelineEntry, format_entry, parse_page, write_page
from brain.paths import BrainPaths
from brain.pipeline.ingest import IngestReport, ingest


class PromoteChatReport(BaseModel):
    """Summary of one promoted AI chat."""

    model_config = ConfigDict(extra="forbid")

    source_event_id: str
    page_slug: str
    page_path: str
    page_event_id: str
    ingest_report: IngestReport
    committed: bool = False


def promote_chat(
    brain_root: Path,
    event_id: str,
    title: str | None = None,
    slug: str | None = None,
) -> PromoteChatReport:
    """Promote one ai_chat ledger event into a conversation page and ingest it."""
    paths = BrainPaths(Path(brain_root))
    source_event = _resolve_event(paths, event_id)
    if source_event.kind is not EventKind.AI_CHAT:
        raise BrainError(f"Event is not an ai_chat event: {source_event.id}")

    raw_text = _read_chat_text(paths.root, source_event)
    _ensure_not_already_promoted(paths, source_event.id)

    draft = llm_client.promote_chat(raw_text, title_hint=title, slug_hint=slug)
    final_title = _clean_title(title or draft.title)
    final_slug = _final_slug(final_title, slug)
    page_path = paths.conversations_dir / f"{source_event.timestamp.date().isoformat()}_{final_slug}.md"
    if page_path.exists():
        raise BrainError(f"Conversation page already exists: {page_path}")

    relative_page_path = page_path.relative_to(paths.root).as_posix()
    _write_conversation_page(
        path=page_path,
        title=final_title,
        slug=final_slug,
        source_event=source_event,
        compiled_truth=draft.compiled_truth,
        timeline_description=draft.timeline_description,
    )

    page_event = _append_page_edited_event(
        paths=paths,
        source_event=source_event,
        page_slug=final_slug,
        relative_page_path=relative_page_path,
    )
    ingest_report = ingest(
        paths.root,
        source="events",
        event_id=page_event.id,
        auto_commit=False,
    )

    committed = False
    if load_config(paths.config_path).git.auto_commit:
        from brain import git_ops

        committed = (
            git_ops.commit(
                paths.root,
                f"promote-chat: {source_event.id} -> {final_slug}",
                paths=_commit_paths(paths, page_path),
            )
            is not None
        )

    return PromoteChatReport(
        source_event_id=source_event.id,
        page_slug=final_slug,
        page_path=relative_page_path,
        page_event_id=page_event.id,
        ingest_report=ingest_report,
        committed=committed,
    )


def _resolve_event(paths: BrainPaths, event_id: str) -> Event:
    if not event_id:
        raise BrainError("event id is required")

    matches = [event for event in read_all(paths.events_jsonl) if event.id.startswith(event_id)]
    if not matches:
        raise BrainError(f"Event not found: {event_id}")
    if len(matches) > 1:
        raise BrainError(f"Event id prefix is ambiguous: {event_id}")
    return matches[0]


def _read_chat_text(root: Path, event: Event) -> str:
    if event.raw_payload is not None:
        return event.raw_payload
    if not event.raw_payload_path:
        raise BrainError(f"AI chat event has no raw payload: {event.id}")

    path = Path(event.raw_payload_path)
    if not path.is_absolute():
        path = root / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BrainError(f"Could not read AI chat payload: {event.raw_payload_path}") from exc


def _ensure_not_already_promoted(paths: BrainPaths, source_event_id: str) -> None:
    if not paths.pages_dir.exists():
        return

    excluded = {paths.pages_index, paths.pages_log}
    for path in sorted(paths.pages_dir.glob("**/*.md")):
        if path in excluded:
            continue
        try:
            page = parse_page(path)
        except PageParseError:
            continue
        if page.frontmatter.external_ids.get("source_event") == source_event_id:
            raise BrainError(f"AI chat event already promoted: {source_event_id}")


def _clean_title(value: str) -> str:
    title = " ".join(value.split())
    if not title:
        raise BrainError("Promoted chat title is empty")
    return title


def _final_slug(title: str, override: str | None) -> str:
    candidate = override if override is not None else _slug_from_title(title)
    if not SLUG_PATTERN.fullmatch(candidate):
        raise BrainError(f"Invalid conversation slug: {candidate}")
    return candidate


def _slug_from_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if not slug or not SLUG_PATTERN.fullmatch(slug):
        raise BrainError(f"Could not derive a valid slug from title: {title}")
    return slug


def _write_conversation_page(
    path: Path,
    title: str,
    slug: str,
    source_event: Event,
    compiled_truth: str,
    timeline_description: str,
) -> None:
    now = _now_utc()
    page = Page(
        frontmatter=Frontmatter(
            type=PageType.CONVERSATION,
            slug=slug,
            title=title,
            created=now,
            updated=now,
            tags=[],
            aliases=[],
            external_ids={"source_event": source_event.id},
        ),
        compiled_truth=compiled_truth.strip(),
        timeline=[
            format_entry(
                TimelineEntry(
                    date=source_event.timestamp.date().isoformat(),
                    event_id=source_event.id,
                    description=_single_line(timeline_description),
                )
            )
        ],
        sources=[f"events.jsonl:{source_event.id}"],
    )
    write_page(path, page)


def _append_page_edited_event(
    paths: BrainPaths,
    source_event: Event,
    page_slug: str,
    relative_page_path: str,
) -> Event:
    event = Event(
        id=str(ulid.ULID()),
        timestamp=_now_utc(),
        kind=EventKind.PAGE_EDITED,
        source_ref=relative_page_path,
        raw_payload_path=relative_page_path,
        affected_pages=[page_slug],
        metadata={
            "action": "promote-chat",
            "source_event": source_event.id,
        },
    )
    append_event(paths.events_jsonl, event)
    return event


def _single_line(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        raise BrainError("Promoted chat timeline description is empty")
    return text


def _commit_paths(paths: BrainPaths, page_path: Path) -> list[Path]:
    candidates = [
        paths.db_path,
        paths.events_jsonl,
        paths.pages_dir,
        paths.review_dir,
        page_path,
    ]
    return [path for path in candidates if path.exists()]


def _now_utc() -> datetime:
    return datetime.now(UTC)
