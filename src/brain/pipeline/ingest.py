from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import ulid
from pydantic import BaseModel, ConfigDict, Field

from brain.config import Config, load_config
from brain.db.backlinks import replace_backlinks_for_page
from brain.db.connection import connect
from brain.db.entities import get_entity
from brain.db.facts import add_fact, find_active_facts, supersede
from brain.db.tier import propose_tier
from brain.exceptions import IngestError
from brain.ledger import append_event, read_all
from brain.models import (
    Entity,
    EntityType,
    Event,
    EventKind,
    Fact,
    FactCandidate,
    FactObjectType,
    Frontmatter,
    Page,
    PageType,
    Tier,
)
from brain.pages import (
    TimelineEntry,
    append_log,
    append_timeline,
    parse_page,
    regenerate_index,
    update_sources,
    write_page,
)
from brain.paths import BrainPaths
from brain.pipeline.autolink import extract_backlinks
from brain.pipeline.conflict import Decision, classify_fact
from brain.pipeline.resolve import resolve_entity
from brain.pipeline.signal_detect import SignalEntity, SignalExtraction, detect_signal
from brain.pipeline.tier import TierProposal, check_tier_upgrade

Source = Literal["laundry", "events", "all"]
VALID_SOURCES = {"laundry", "events", "all"}
REVIEW_KINDS = {
    "fact_conflict",
    "low_confidence_fact",
    "tier_proposal",
    "new_entity_review",
}
REVIEW_DECISION_SECTION = """## Decision

[ ] approve
[ ] reject
[ ] defer
"""

class IngestReport(BaseModel):
    """Summary of one ingest run."""

    model_config = ConfigDict(extra="forbid")

    processed: int = 0
    facts_added: int = 0
    review_items_created: int = 0
    pages_touched: list[str] = Field(default_factory=list)
    laundry_archived: int = 0
    dry_run: bool = False
    review_files: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class IngestItem:
    source: Literal["laundry", "events"]
    source_ref: str
    text: str
    event: Event
    laundry_path: Path | None = None


@dataclass
class ItemResult:
    fact_ids: list[str] = field(default_factory=list)
    page_slugs: set[str] = field(default_factory=set)
    page_paths: set[Path] = field(default_factory=set)
    timeline_written: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class ReviewWriter:
    paths: BrainPaths
    report: IngestReport
    date: str
    created_at: datetime
    next_seq: int

    @classmethod
    def create(cls, paths: BrainPaths, report: IngestReport) -> ReviewWriter:
        created_at = _now_utc()
        date = created_at.date().isoformat()
        return cls(
            paths=paths,
            report=report,
            date=date,
            created_at=created_at,
            next_seq=_next_review_seq(paths.review_dir, date),
        )

    def write(self, kind: str, body: str) -> str:
        if kind not in REVIEW_KINDS:
            raise IngestError(f"Unsupported review kind: {kind}")

        review_id = f"{self.date}_{self.next_seq:03d}_{kind}"
        self.next_seq += 1
        path = self.paths.review_dir / f"{review_id}.md"
        metadata = "\n".join(
            [
                "---",
                f"review_id: {review_id}",
                f"kind: {kind}",
                f"created: {self.created_at.isoformat()}",
                "status: pending",
                "---",
                "",
            ]
        )
        _write_lf(path, metadata + _with_decision_section(body))

        relative = path.relative_to(self.paths.root).as_posix()
        self.report.review_items_created += 1
        self.report.review_files.append(relative)
        return relative


def ingest(
    brain_root: Path,
    source: str = "all",
    dry_run: bool = False,
    limit: int | None = None,
    event_id: str | None = None,
    auto_commit: bool | None = None,
) -> IngestReport:
    """Run the core ingest pipeline for laundry files and/or ledger events."""
    if source not in VALID_SOURCES:
        raise IngestError(f"Unsupported ingest source: {source}")
    if limit is not None and limit < 0:
        raise IngestError("limit must be non-negative")
    if event_id is not None and source not in {"events", "all"}:
        raise IngestError("event_id can only be used with events or all source")

    paths = BrainPaths(Path(brain_root))
    config = load_config(paths.config_path)
    report = IngestReport(dry_run=dry_run)

    if dry_run:
        return _run_dry_ingest(paths, source, limit, report, event_id=event_id)

    conn = _connect_for_ingest(paths.db_path, dry_run=dry_run)
    try:
        items = _collect_items(paths, conn, source, limit, event_id=event_id)
        review_writer = ReviewWriter.create(paths, report)

        for item in items:
            try:
                extraction = _detect_item_signal(item)
                with conn:
                    result = _apply_extraction(
                        conn=conn,
                        paths=paths,
                        config=config,
                        item=item,
                        extraction=extraction,
                        review_writer=review_writer,
                        report=report,
                    )

                _record_item_success(paths, conn, item, result, report)
            except Exception as exc:
                report.errors.append(f"{item.source_ref}: {exc}")
                if not dry_run and item.source == "events":
                    _set_cursor(conn, "events", item.event.id)

        _finalize_run(conn, paths, config, report, auto_commit=auto_commit)
    finally:
        _close_ingest_connection(conn, paths.db_path)

    return _sorted_report(report)


def _run_dry_ingest(
    paths: BrainPaths,
    source: str,
    limit: int | None,
    report: IngestReport,
    *,
    event_id: str | None = None,
) -> IngestReport:
    items = _collect_dry_items(paths, source, limit, event_id=event_id)
    for item in items:
        try:
            _detect_item_signal(item)
            report.processed += 1
        except Exception as exc:  # per-item ingest failures are reportable
            report.errors.append(f"{item.source_ref}: {exc}")
    return _sorted_report(report)


def _collect_items(
    paths: BrainPaths,
    conn: sqlite3.Connection,
    source: str,
    limit: int | None,
    *,
    event_id: str | None = None,
) -> list[IngestItem]:
    items: list[IngestItem] = []
    if source in {"laundry", "all"}:
        items.extend(_collect_laundry_items(paths))
    if source in {"events", "all"}:
        items.extend(_collect_event_items(paths, conn, event_id=event_id))

    if limit is None:
        return items
    return items[:limit]


def _collect_dry_items(
    paths: BrainPaths,
    source: str,
    limit: int | None,
    *,
    event_id: str | None = None,
) -> list[IngestItem]:
    items: list[IngestItem] = []
    if source in {"laundry", "all"}:
        items.extend(_collect_laundry_items(paths))
    if source in {"events", "all"}:
        items.extend(_collect_event_items_without_cursor(paths, event_id=event_id))

    if limit is None:
        return items
    return items[:limit]


def _collect_laundry_items(paths: BrainPaths) -> list[IngestItem]:
    if not paths.laundry_dir.exists():
        return []

    files = sorted(
        path
        for path in paths.laundry_dir.rglob("*")
        if path.is_file() and _is_unprocessed_laundry_file(paths, path)
    )

    items: list[IngestItem] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        source_ref = f"laundry/{path.name}"
        event = Event(
            id=str(ulid.ULID()),
            timestamp=_now_utc(),
            kind=EventKind.LAUNDRY_INGESTED,
            source_ref=source_ref,
            raw_payload=text,
        )
        items.append(
            IngestItem(
                source="laundry",
                source_ref=source_ref,
                text=text,
                event=event,
                laundry_path=path,
            )
        )
    return items


def _is_unprocessed_laundry_file(paths: BrainPaths, path: Path) -> bool:
    try:
        relative = path.relative_to(paths.laundry_dir)
    except ValueError:
        return False
    return not relative.parts or relative.parts[0] != paths.laundry_processed_dir.name


def _collect_event_items(
    paths: BrainPaths,
    conn: sqlite3.Connection,
    *,
    event_id: str | None = None,
) -> list[IngestItem]:
    last_processed = _get_cursor(conn, "events")
    items: list[IngestItem] = []
    for event in read_all(paths.events_jsonl):
        if event_id is not None and event.id != event_id:
            continue
        if event_id is None and last_processed is not None and event.id <= last_processed:
            continue
        if event.kind is EventKind.LAUNDRY_INGESTED:
            continue
        text = _event_text(paths.root, event)
        items.append(
            IngestItem(
                source="events",
                source_ref=event.source_ref,
                text=text,
                event=event,
            )
        )
    return items


def _collect_event_items_without_cursor(
    paths: BrainPaths,
    *,
    event_id: str | None = None,
) -> list[IngestItem]:
    items: list[IngestItem] = []
    for event in read_all(paths.events_jsonl):
        if event_id is not None and event.id != event_id:
            continue
        if event.kind is EventKind.LAUNDRY_INGESTED:
            continue
        text = _event_text(paths.root, event)
        items.append(
            IngestItem(
                source="events",
                source_ref=event.source_ref,
                text=text,
                event=event,
            )
        )
    return items


def _event_text(root: Path, event: Event) -> str:
    if event.raw_payload:
        return event.raw_payload
    if event.raw_payload_path:
        path = Path(event.raw_payload_path)
        if not path.is_absolute():
            path = root / path
        return path.read_text(encoding="utf-8")
    raise IngestError(f"Event has no raw payload: {event.id}")


def _detect_item_signal(item: IngestItem) -> SignalExtraction:
    extraction = detect_signal(
        item.text,
        hint={
            "source": item.source,
            "source_ref": item.source_ref,
            "source_event": item.event.id,
        },
    )
    return _normalize_extraction_sources(extraction, item)


def _normalize_extraction_sources(
    extraction: SignalExtraction,
    item: IngestItem,
) -> SignalExtraction:
    facts = [
        fact.model_copy(
            update={
                "source_event": item.event.id,
                "source_ref": item.event.source_ref,
            }
        )
        for fact in extraction.facts
    ]
    return extraction.model_copy(update={"facts": facts})


def _apply_extraction(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    config: Config,
    item: IngestItem,
    extraction: SignalExtraction,
    review_writer: ReviewWriter,
    report: IngestReport,
) -> ItemResult:
    result = ItemResult()
    entity_map, unresolved = _resolve_entities(
        conn,
        extraction.entities,
        review_writer,
    )

    _write_tier_proposals(
        conn,
        config,
        sorted(set(entity_map.values())),
        review_writer,
    )

    for candidate in extraction.facts:
        normalized = _normalize_candidate(
            conn=conn,
            candidate=candidate,
            entity_map=entity_map,
            unresolved=unresolved,
            review_writer=review_writer,
        )
        if normalized is None:
            continue

        _handle_candidate(
            conn=conn,
            paths=paths,
            config=config,
            item=item,
            candidate=normalized,
            timeline_summary=extraction.timeline_summary,
            review_writer=review_writer,
            report=report,
            result=result,
        )

    report.processed += 1
    return result


def _resolve_entities(
    conn: sqlite3.Connection,
    signal_entities: list[SignalEntity],
    review_writer: ReviewWriter,
) -> tuple[dict[str, str], set[str]]:
    entity_map: dict[str, str] = {}
    unresolved: set[str] = set()

    for signal_entity in _unique_signal_entities(signal_entities):
        entity = resolve_entity(conn, signal_entity.name, signal_entity.type)
        if entity is None:
            unresolved.add(signal_entity.name)
            _write_new_entity_review(review_writer, signal_entity)
            continue
        entity_map[signal_entity.name] = entity.id
        entity_map[entity.title] = entity.id
        entity_map[entity.id] = entity.id

    return entity_map, unresolved


def _unique_signal_entities(signal_entities: list[SignalEntity]) -> list[SignalEntity]:
    seen: set[str] = set()
    unique: list[SignalEntity] = []
    for signal_entity in signal_entities:
        if signal_entity.name in seen:
            continue
        seen.add(signal_entity.name)
        unique.append(signal_entity)
    return unique


def _normalize_candidate(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
    entity_map: dict[str, str],
    unresolved: set[str],
    review_writer: ReviewWriter,
) -> FactCandidate | None:
    subject = _candidate_entity_id(
        conn=conn,
        name=candidate.subject,
        hint_type=None,
        entity_map=entity_map,
        unresolved=unresolved,
        review_writer=review_writer,
    )
    if subject is None:
        return None

    object_value = candidate.object
    if candidate.object_type is FactObjectType.ENTITY:
        resolved_object = _candidate_entity_id(
            conn=conn,
            name=candidate.object,
            hint_type=None,
            entity_map=entity_map,
            unresolved=unresolved,
            review_writer=review_writer,
        )
        if resolved_object is None:
            return None
        object_value = resolved_object

    return candidate.model_copy(
        update={
            "subject": subject,
            "object": object_value,
        }
    )


def _candidate_entity_id(
    conn: sqlite3.Connection,
    name: str,
    hint_type: EntityType | None,
    entity_map: dict[str, str],
    unresolved: set[str],
    review_writer: ReviewWriter,
) -> str | None:
    if name in unresolved:
        return None
    if name in entity_map:
        return entity_map[name]

    entity = resolve_entity(conn, name, hint_type)
    if entity is None:
        unresolved.add(name)
        _write_new_entity_review(
            review_writer,
            SignalEntity(name=name, type=hint_type, confidence=1.0),
        )
        return None

    entity_map[name] = entity.id
    entity_map[entity.title] = entity.id
    entity_map[entity.id] = entity.id
    return entity.id


def _handle_candidate(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    config: Config,
    item: IngestItem,
    candidate: FactCandidate,
    timeline_summary: str,
    review_writer: ReviewWriter,
    report: IngestReport,
    result: ItemResult,
) -> None:
    auto_accept = config.ingest.confidence_auto_accept
    auto_reject = config.ingest.confidence_auto_reject

    if candidate.confidence < auto_reject:
        return
    if candidate.confidence < auto_accept:
        _write_low_confidence_review(review_writer, candidate)
        return

    decision = classify_fact(conn, candidate, config)
    if decision is Decision.NOOP:
        return
    if decision is Decision.CONFLICT:
        _write_fact_conflict_review(
            review_writer,
            candidate,
            find_active_facts(conn, candidate.subject, candidate.predicate),
        )
        return

    old_active = find_active_facts(conn, candidate.subject, candidate.predicate)
    fact_id = _add_candidate_fact(conn, candidate, item.event.timestamp)
    report.facts_added += 1
    result.fact_ids.append(str(fact_id))

    if decision is Decision.SUPERSEDE:
        for old_fact in old_active:
            if old_fact.id is not None:
                supersede(conn, old_fact.id, fact_id)

    _touch_subject_page(
        paths=paths,
        conn=conn,
        subject_id=candidate.subject,
        source_ref=item.source_ref,
        event_id=item.event.id,
        event_date=item.event.timestamp.date().isoformat(),
        timeline_summary=timeline_summary,
        report=report,
        result=result,
    )


def _add_candidate_fact(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
    asserted_at: datetime,
) -> int:
    fact = Fact(
        subject=candidate.subject,
        predicate=candidate.predicate,
        object=candidate.object,
        object_type=candidate.object_type,
        valid_from=candidate.valid_from,
        valid_to=candidate.valid_to,
        asserted_at=asserted_at,
        source_event=candidate.source_event,
        source_ref=candidate.source_ref,
        confidence=candidate.confidence,
    )
    return add_fact(conn, fact)


def _touch_subject_page(
    paths: BrainPaths,
    conn: sqlite3.Connection,
    subject_id: str,
    source_ref: str,
    event_id: str,
    event_date: str,
    timeline_summary: str,
    report: IngestReport,
    result: ItemResult,
) -> None:
    entity = get_entity(conn, subject_id)
    if entity is None:
        raise IngestError(f"Cannot touch page for missing entity: {subject_id}")

    page_path = paths.entities_dir / f"{entity.id}.md"
    if not page_path.exists():
        _write_stub_page(page_path, entity, source_ref)
    else:
        page = parse_page(page_path)
        if source_ref not in page.sources:
            update_sources(page_path, [*page.sources, source_ref])

    timeline_key = (entity.id, event_id)
    if timeline_key not in result.timeline_written:
        append_timeline(
            page_path,
            TimelineEntry(
                date=event_date,
                event_id=event_id,
                description=timeline_summary,
            ),
        )
        result.timeline_written.add(timeline_key)

    relative = page_path.relative_to(paths.root).as_posix()
    _append_unique(report.pages_touched, relative)
    result.page_paths.add(page_path)
    result.page_slugs.add(entity.id)


def _write_stub_page(path: Path, entity: Entity, source_ref: str) -> None:
    now = _now_utc()
    page = Page(
        frontmatter=Frontmatter(
            type=PageType.ENTITY,
            slug=entity.id,
            title=entity.title,
            tier=entity.tier if isinstance(entity.tier, Tier) else Tier(entity.tier),
            created=now,
            updated=now,
            tags=[],
            aliases=[],
            external_ids={},
        ),
        compiled_truth="(stub - waiting for more evidence)",
        timeline=[],
        sources=[source_ref],
    )
    write_page(path, page)


def _write_tier_proposals(
    conn: sqlite3.Connection,
    config: Config,
    entity_ids: list[str],
    review_writer: ReviewWriter,
) -> None:
    for entity_id in entity_ids:
        proposal = check_tier_upgrade(conn, entity_id, config)
        if proposal is None or _has_pending_tier_proposal(conn, proposal):
            continue

        review_file = _write_tier_review(review_writer, proposal)
        propose_tier(
            conn,
            entity_id=proposal.entity_id,
            target_tier=proposal.proposed_tier,
            reason=proposal.reason,
            review_file=review_file,
        )


def _has_pending_tier_proposal(
    conn: sqlite3.Connection,
    proposal: TierProposal,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM tier_proposals
        WHERE entity_id = ?
          AND proposed_tier = ?
          AND decision IS NULL
        LIMIT 1
        """,
        (proposal.entity_id, int(proposal.proposed_tier)),
    ).fetchone()
    return row is not None


def _write_new_entity_review(
    review_writer: ReviewWriter,
    signal_entity: SignalEntity,
) -> None:
    body = "\n".join(
        [
            "# New entity needs review",
            "",
            f"- name: {signal_entity.name}",
            f"- type: {_enum_value(signal_entity.type)}",
            f"- confidence: {signal_entity.confidence}",
            "",
            "Create a canonical ASCII slug or merge this mention into an existing entity.",
        ]
    )
    review_writer.write("new_entity_review", body)


def _write_low_confidence_review(
    review_writer: ReviewWriter,
    candidate: FactCandidate,
) -> None:
    body = "\n".join(
        [
            "# Low confidence fact",
            "",
            "```json",
            json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False, indent=2),
            "```",
        ]
    )
    review_writer.write("low_confidence_fact", body)


def _write_fact_conflict_review(
    review_writer: ReviewWriter,
    candidate: FactCandidate,
    active_facts: list[Fact],
) -> None:
    body = "\n".join(
        [
            "# Fact conflict",
            "",
            "## Candidate",
            "",
            "```json",
            json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Active facts",
            "",
            "```json",
            json.dumps(
                [fact.model_dump(mode="json") for fact in active_facts],
                ensure_ascii=False,
                indent=2,
            ),
            "```",
        ]
    )
    review_writer.write("fact_conflict", body)


def _write_tier_review(
    review_writer: ReviewWriter,
    proposal: TierProposal,
) -> str:
    body = "\n".join(
        [
            "# Tier proposal",
            "",
            f"- entity_id: {proposal.entity_id}",
            f"- current_tier: {int(proposal.current_tier)}",
            f"- proposed_tier: {int(proposal.proposed_tier)}",
            f"- mention_count: {proposal.mention_count}",
            f"- reason: {proposal.reason}",
        ]
    )
    return review_writer.write("tier_proposal", body)


def _record_item_success(
    paths: BrainPaths,
    conn: sqlite3.Connection,
    item: IngestItem,
    result: ItemResult,
    report: IngestReport,
) -> None:
    if item.source == "laundry":
        event = item.event.model_copy(
            update={
                "extracted_facts": result.fact_ids,
                "affected_pages": sorted(result.page_slugs),
            }
        )
        append_event(paths.events_jsonl, event)
        if item.laundry_path is None:
            raise IngestError("Laundry item is missing its source path")
        _archive_laundry_item(paths, item.laundry_path)
        report.laundry_archived += 1
        _set_cursor(conn, "laundry", item.source_ref)
        return

    _set_cursor(conn, "events", item.event.id)


def _archive_laundry_item(paths: BrainPaths, path: Path) -> None:
    paths.laundry_processed_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_processed_path(paths.laundry_processed_dir / path.name)
    shutil.move(str(path), str(target))


def _unique_processed_path(path: Path) -> Path:
    if not path.exists():
        return path

    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise IngestError(f"Could not find archive name for {path}")


def _finalize_run(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    config: Config,
    report: IngestReport,
    *,
    auto_commit: bool | None = None,
) -> None:
    with conn:
        _rebuild_touched_backlinks(conn, paths, report.pages_touched)

    regenerate_index(paths.root)
    append_log(
        paths.root,
        (
            f"- {_now_utc().strftime('%Y-%m-%d %H:%M')} ingest: "
            f"{report.processed} events processed, "
            f"{report.facts_added} facts added, "
            f"{report.review_items_created} review items created"
        ),
    )

    should_commit = config.git.auto_commit if auto_commit is None else auto_commit
    if should_commit:
        _checkpoint_db(conn)
        from brain import git_ops

        git_ops.commit(
            paths.root,
            f"ingest: process {report.processed} items",
            paths=_commit_paths(paths),
        )


def _rebuild_touched_backlinks(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    touched_pages: list[str],
) -> None:
    if not touched_pages:
        return

    alias_map, entity_types = _load_alias_map(conn)
    for relative in touched_pages:
        page_path = paths.root / relative
        page = parse_page(page_path)
        content = page_path.read_text(encoding="utf-8")
        links = extract_backlinks(
            content,
            alias_map=alias_map,
            from_page=page.frontmatter.slug,
            from_page_type=page.frontmatter.type,
            entity_types=entity_types,
        )
        links = [link for link in links if link.to_entity in entity_types]
        replace_backlinks_for_page(conn, page.frontmatter.slug, links)


def _load_alias_map(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[str, EntityType]]:
    alias_rows = conn.execute("SELECT alias, entity_id FROM entity_aliases").fetchall()
    entity_rows = conn.execute("SELECT id, title, type FROM entities").fetchall()

    alias_map = {row["alias"]: row["entity_id"] for row in alias_rows}
    entity_types: dict[str, EntityType] = {}
    for row in entity_rows:
        entity_id = row["id"]
        alias_map.setdefault(row["title"], entity_id)
        alias_map.setdefault(entity_id, entity_id)
        entity_types[entity_id] = EntityType(row["type"])
    return alias_map, entity_types


def _get_cursor(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT last_processed FROM ingest_cursor WHERE source = ?",
        (source,),
    ).fetchone()
    if row is None:
        return None
    return row["last_processed"]


def _connect_for_ingest(path: Path, *, dry_run: bool) -> sqlite3.Connection:
    if not dry_run:
        return connect(path)

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _close_ingest_connection(conn: sqlite3.Connection, db_path: Path) -> None:
    try:
        _checkpoint_db(conn)
    finally:
        conn.close()
        _remove_sqlite_sidecars(db_path)


def _checkpoint_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _remove_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        try:
            sidecar.unlink()
        except (FileNotFoundError, PermissionError):
            continue


def _commit_paths(paths: BrainPaths) -> list[Path]:
    candidates = [
        paths.db_path,
        paths.events_jsonl,
        paths.laundry_dir,
        paths.pages_dir,
        paths.review_dir,
    ]
    return [path for path in candidates if path.exists()]


def _set_cursor(conn: sqlite3.Connection, source: str, last_processed: str) -> None:
    conn.execute(
        """
        INSERT INTO ingest_cursor (source, last_processed, last_run_at)
        VALUES (?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            last_processed = excluded.last_processed,
            last_run_at = excluded.last_run_at
        """,
        (source, last_processed, _now_utc().isoformat()),
    )
    conn.commit()


def _next_review_seq(review_dir: Path, date: str) -> int:
    if not review_dir.exists():
        return 1

    max_seq = 0
    for path in review_dir.glob(f"{date}_*_*.md"):
        parts = path.stem.split("_", maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            max_seq = max(max_seq, int(parts[1]))
        except ValueError:
            continue
    return max_seq + 1


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _sorted_report(report: IngestReport) -> IngestReport:
    report.pages_touched = sorted(report.pages_touched)
    report.review_files = sorted(report.review_files)
    return report


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    path.write_text(normalized, encoding="utf-8", newline="\n")


def _with_decision_section(body: str) -> str:
    stripped = body.strip()
    if "## Decision" in stripped:
        return f"{stripped}\n"
    return f"{stripped}\n\n{REVIEW_DECISION_SECTION}"
