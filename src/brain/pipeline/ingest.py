from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import ulid
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from brain.concurrency import coordinated, operation_lock, root_lock
from brain.config import Config, load_config
from brain.db.backlinks import replace_backlinks_for_page
from brain.db.connection import connect, connect_readonly
from brain.db.entities import get_entity
from brain.db.facts import add_fact, find_active_facts, supersede
from brain.db.tier import propose_tier
from brain.exceptions import BrainError, ConfigError, IngestError, LLMError
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
)
from brain.models.page import SLUG_PATTERN
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
from brain.pipeline.reindex import reindex
from brain.pipeline.resolve import (
    _slug_from_name,
    matching_entity_ids,
    normalize_name,
    resolve_entity,
)
from brain.pipeline.signal_detect import (
    ProcedureCandidate,
    SignalEntity,
    SignalExtraction,
    detect_signal,
)
from brain.pipeline.tier import TierProposal, check_tier_upgrade
from brain.predicates import fact_sentence, normalize_predicate, uses_chinese
from brain.privacy import external_allowed, split_provenance
from brain.transactions import (
    atomic_text,
    completed,
    durable_unit,
    ensure_operation_table,
    protect_path,
)

Source = Literal["laundry", "events", "all"]
VALID_SOURCES = {"laundry", "events", "all"}
REVIEW_KINDS = {
    "fact_conflict",
    "ingest_error",
    "low_confidence_fact",
    "pending_fact",
    "procedure_candidate",
    "tier_proposal",
    "new_entity_review",
    "summary_refresh",
}
REVIEW_DECISION_SECTION = """## Decision

[ ] approve
[ ] reject
[ ] defer
"""
FAILED_LAUNDRY_DIR_NAME = "failed"
BRAIN_CONFIG_ENV = "BRAIN_CONFIG"
TRANSIENT_ENTITY_PAGE_MIN_MENTIONS = 2
TRANSIENT_ENTITY_TERMS = {
    "draft",
    "note",
    "notes",
    "plan",
    "release",
    "review",
    "smoke",
    "test",
    "testing",
    "todo",
    "upgrade",
    "verification",
    "verify",
}
TRANSIENT_ENTITY_PATTERNS = (
    re.compile(r"^v?\d+(?:[-.]\d+){1,4}$", re.IGNORECASE),
    re.compile(r"^\d{4}[-.]?\d{2}[-.]?\d{2}$"),
    re.compile(r"\b\d{8}\b"),
    re.compile(r"\.(?:md|txt|json|toml|ya?ml|py|ts|tsx|js|jsx|pdf|docx?)$", re.IGNORECASE),
)


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
    skipped_private: int = 0


class RequeueReport(BaseModel):
    """Summary of failed laundry moved back to the pending queue."""

    model_config = ConfigDict(extra="forbid")

    requeued: int = 0
    files: list[str] = Field(default_factory=list)


class IngestFailureKind(StrEnum):
    """Operational scope of a failure raised while processing one item."""

    CONTENT = "content"
    INFRASTRUCTURE = "infrastructure"
    TRANSIENT = "transient"


@dataclass(frozen=True)
class IngestItem:
    source: Literal["laundry", "events"]
    source_ref: str
    text: str
    event: Event
    laundry_path: Path | None = None
    raw_hash: str = ""
    hints: dict = field(default_factory=dict)
    read_error: str | None = None


@dataclass(frozen=True)
class StagedExtraction:
    item: IngestItem
    extraction: SignalExtraction | None = None
    error: Exception | None = None


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
    auto_reindex: bool | None = None,
) -> IngestReport:
    """Run the core ingest pipeline for laundry files and/or ledger events."""
    if source not in VALID_SOURCES:
        raise IngestError(f"Unsupported ingest source: {source}")
    if limit is not None and limit < 0:
        raise IngestError("limit must be non-negative")
    if event_id is not None and source not in {"events", "all"}:
        raise IngestError("event_id can only be used with events or all source")

    paths = BrainPaths(Path(brain_root))
    report = IngestReport(dry_run=dry_run)
    if dry_run:
        with root_lock(paths.root):
            return _run_dry_ingest(paths, source, limit, report, event_id=event_id)

    # Serialize ingesters, but release the data-root lock during provider work.
    with operation_lock(paths.root, "ingest"), _configured_llm_path(paths.config_path):
        with root_lock(paths.root), closing(connect(paths.db_path, read_only=True)) as conn:
            config = load_config(paths.config_path)
            config_hash = hashlib.sha256(paths.config_path.read_bytes()).hexdigest()
            items = _collect_items(paths, conn, source, limit, event_id=event_id)
            items = [_with_entity_hints(paths, conn, item, config) for item in items]
            permitted = [item for item in items if item.read_error or external_allowed(
                paths.root, path=item.laundry_path or item.event.raw_payload_path or item.source_ref,
                text=item.text, metadata=item.event.metadata)]
            report.skipped_private = len(items) - len(permitted)
        if any(not item.read_error for item in permitted):
            _preflight_ingest_provider(config)
        staged = _stage_extractions(permitted, paths=paths, config_hash=config_hash)
        for staged_item in staged:
            with root_lock(paths.root, write=True), closing(connect(paths.db_path)) as conn:
                ensure_operation_table(conn)
                item = staged_item.item
                key = _item_key(item)
                if completed(conn, key) or (event_id is None and item.source == "events" and completed(conn, "failed:" + key)):
                    continue
                if hashlib.sha256(paths.config_path.read_bytes()).hexdigest() != config_hash:
                    raise IngestError("Configuration changed during extraction; rerun with the new policy")
                if item.laundry_path is not None:
                    if not item.laundry_path.is_file():
                        continue
                    if hashlib.sha256(item.laundry_path.read_bytes()).hexdigest() != item.raw_hash:
                        raise IngestError("Source changed during extraction; cached result was not applied")
                    item = _with_archive_ref(paths, item, failed=staged_item.error is not None)
                if item.source == "events" and _event_text(paths.root, item.event) != item.text:
                    raise IngestError("Event source changed during extraction; cached result was not applied")
                if not item.read_error and not external_allowed(paths.root, path=item.laundry_path or item.source_ref,
                                        text=item.text, metadata=item.event.metadata):
                    report.skipped_private += 1
                    continue
                writer = ReviewWriter.create(paths, report)
                before = report.model_copy(deep=True)
                try:
                    unit_key = key if staged_item.error is None else "failed:" + key + ":" + str(ulid.ULID())
                    with durable_unit(paths.root, conn, unit_key):
                        if staged_item.error is not None:
                            _record_item_content_failure(paths, conn, item, staged_item.error,
                                                         writer, report, advance_cursor=False)
                            if item.source == "events":
                                conn.execute("INSERT OR IGNORE INTO operation_commits (id) VALUES (?)", ("failed:" + key,))
                        else:
                            assert staged_item.extraction is not None
                            extraction = _normalize_extraction_sources(staged_item.extraction, item)
                            result = _apply_extraction(conn=conn, paths=paths, config=config, item=item,
                                                       extraction=extraction, review_writer=writer, report=report)
                            _record_item_success(paths, conn, item, result, report, advance_cursor=False)
                            _rebuild_touched_backlinks(conn, paths, report.pages_touched)
                            regenerate_index(paths.root)
                except Exception as exc:
                    report = before
                    raise _batch_ingest_error(item, exc, _classify_ingest_failure(exc)) from exc
        with root_lock(paths.root, write=True), closing(connect(paths.db_path)) as conn:
            # A skipped local-only event must not be passed by the global cursor.
            if event_id is None and source in {"all", "events"}:
                _advance_completed_events(paths, conn)
            if report.processed or report.review_items_created:
                _finalize_run(conn, paths, config, report, auto_commit=auto_commit, auto_reindex=False)
        should_reindex = config.import_.auto_reindex if auto_reindex is None else auto_reindex
        if should_reindex and report.pages_touched:
            _run_auto_reindex(paths, report)
    return _sorted_report(report)


def _item_key(item: IngestItem) -> str:
    origin = str(item.laundry_path.resolve()) if item.laundry_path else item.event.id
    return hashlib.sha256((origin + "\0" + (item.raw_hash if item.laundry_path else "")).encode()).hexdigest()


def _with_entity_hints(paths, conn, item, config):
    aliases, types = _load_alias_map(conn)
    candidates = []
    seen = set()
    folded = item.text.casefold()
    for name, entity_id in aliases.items():
        if len(name) < 2 or name.casefold() not in folded or entity_id in seen:
            continue
        entity = get_entity(conn, entity_id)
        if entity and external_allowed(paths.root, path=entity.page_path):
            candidates.append({"id": entity_id, "name": entity.title, "type": types[entity_id].value})
            seen.add(entity_id)
        if len(candidates) >= 30:
            break
    return replace(item, hints={"existing_entities": candidates,
                                "output_language": config.ingest.output_language})


def _with_archive_ref(paths: BrainPaths, item: IngestItem, *, failed: bool = False) -> IngestItem:
    assert item.laundry_path is not None
    relative = item.laundry_path.relative_to(paths.laundry_dir)
    directory = paths.laundry_dir / (FAILED_LAUNDRY_DIR_NAME if failed else "processed")
    target = _unique_processed_path(directory / relative)
    ref = target.relative_to(paths.root).as_posix()
    return replace(item, source_ref=ref, event=item.event.model_copy(update={"source_ref": ref}))


def _advance_completed_events(paths, conn):
    ensure_operation_table(conn)
    cursor = _get_cursor(conn, "events")
    last = None
    for event in read_all(paths.events_jsonl):
        if cursor and event.id <= cursor:
            continue
        if event.kind not in {EventKind.BULK_IMPORTED, EventKind.LAUNDRY_INGESTED} and _event_has_payload(event):
            item = IngestItem("events", event.source_ref, _event_text(paths.root, event), event)
            if not completed(conn, _item_key(item)) and not completed(conn, "failed:" + _item_key(item)):
                break
        last = event.id
    if last:
        _set_cursor(conn, "events", last)
        conn.commit()


@coordinated(write=True)
def requeue_failed_laundry(
    brain_root: Path,
    *,
    limit: int | None = None,
) -> RequeueReport:
    """Move failed laundry back to the pending queue without overwriting files.

    Requeue is deliberately separate from provider-backed ingest. The caller can
    inspect the restored files and invoke ingest explicitly when ready.
    """
    if limit is not None and limit <= 0:
        raise IngestError("limit must be positive")

    paths = BrainPaths(Path(brain_root))
    failed_dir = paths.laundry_dir / FAILED_LAUNDRY_DIR_NAME
    if not failed_dir.exists():
        return RequeueReport()

    failed_files = sorted(path for path in failed_dir.rglob("*") if path.is_file())
    if limit is not None:
        failed_files = failed_files[:limit]

    paths.laundry_dir.mkdir(parents=True, exist_ok=True)
    report = RequeueReport()
    for source_path in failed_files:
        target = _unique_processed_path(paths.laundry_dir / source_path.name)
        shutil.move(str(source_path), str(target))
        report.requeued += 1
        report.files.append(target.relative_to(paths.root).as_posix())

    report.files.sort()
    return report


def _run_dry_ingest(
    paths: BrainPaths,
    source: str,
    limit: int | None,
    report: IngestReport,
    *,
    event_id: str | None = None,
) -> IngestReport:
    items = _collect_dry_items(paths, source, limit, event_id=event_id)
    report.processed = len(items)
    return _sorted_report(report)


def _stage_extractions(items: list[IngestItem], *, paths: BrainPaths | None = None,
                       config_hash: str = "") -> list[StagedExtraction]:
    staged: list[StagedExtraction] = []
    for item in items:
        cache = None
        if paths is not None:
            cache_key = hashlib.sha256((_item_key(item) + hashlib.sha256(item.text.encode()).hexdigest() + config_hash + "v3").encode()).hexdigest()
            cache = paths.root / ".brainmem" / "extractions" / f"{cache_key}.json"
            if cache.is_file():
                saved = json.loads(cache.read_text(encoding="utf-8"))
                item = replace(item, event=Event.model_validate(saved["event"]))
                staged.append(StagedExtraction(item, SignalExtraction.model_validate(saved["extraction"])))
                continue
        try:
            if item.read_error:
                raise IngestError(item.read_error)
            extraction = _detect_item_signal(item)
        except Exception as exc:
            failure_kind = _classify_ingest_failure(exc)
            if failure_kind is not IngestFailureKind.CONTENT:
                raise _batch_ingest_error(item, exc, failure_kind) from exc
            staged.append(StagedExtraction(item=item, error=exc))
            continue
        staged.append(StagedExtraction(item=item, extraction=extraction))
        if cache is not None:
            atomic_text(cache, json.dumps({"event": item.event.model_dump(mode="json", exclude={"raw_payload"}),
                                          "extraction": extraction.model_dump(mode="json")}, ensure_ascii=False))
    return staged


def _record_item_content_failure(
    paths: BrainPaths,
    conn: sqlite3.Connection,
    item: IngestItem,
    exc: Exception,
    review_writer: ReviewWriter,
    report: IngestReport,
    *,
    advance_cursor: bool = True,
) -> None:
    report.errors.append(f"{item.source_ref}: {exc}")
    _write_ingest_error_review(
        review_writer,
        item,
        exc,
        failure_kind=IngestFailureKind.CONTENT,
    )
    if item.source == "events":
        if advance_cursor:
            _set_cursor(conn, "events", item.event.id)
    elif item.laundry_path is not None:
        _move_protected(item.laundry_path, paths.root / item.source_ref)


def _preflight_ingest_provider(config: Config) -> None:
    if config.deepseek is not None:
        _require_provider_key("deepseek", config.deepseek.api_key_env)
        _validate_provider_endpoint("deepseek", config.deepseek.base_url)
        return
    if config.openai is not None:
        _require_provider_key("openai", config.openai.api_key_env)
        return
    if config.anthropic is not None:
        _require_provider_key("anthropic", config.anthropic.api_key_env)
        return
    raise IngestError("Ingest provider preflight failed: no LLM provider is configured")


def _require_provider_key(provider: str, env_name: str) -> None:
    if os.environ.get(env_name, "").strip():
        return
    raise IngestError(
        "Ingest provider preflight failed: "
        f"{provider} API key environment variable {env_name!r} is not set. "
        "No source items were processed and laundry remains pending."
    )


def _validate_provider_endpoint(provider: str, endpoint: str) -> None:
    try:
        parsed = urlsplit(endpoint)
        has_host = bool(parsed.hostname)
    except ValueError as exc:
        raise IngestError(
            f"Ingest provider preflight failed: {provider} base_url is invalid"
        ) from exc

    if (
        endpoint != endpoint.strip()
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not has_host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise IngestError(
            f"Ingest provider preflight failed: {provider} base_url must be a valid "
            "HTTP(S) endpoint without embedded credentials"
        )


@contextmanager
def _configured_llm_path(config_path: Path) -> Iterator[None]:
    from brain.config_context import configured_path

    with configured_path(config_path):
        yield


def _classify_ingest_failure(exc: Exception) -> IngestFailureKind:
    chain = _exception_chain(exc)

    if any(
        isinstance(error, (ValidationError, json.JSONDecodeError, UnicodeError)) for error in chain
    ):
        return IngestFailureKind.CONTENT

    messages = " ".join(str(error).lower() for error in chain)
    if any(isinstance(error, LLMError) for error in chain):
        if any(
            marker in messages
            for marker in (
                "response was not valid json",
                "response was not structured json",
                "response did not contain text",
                "failed the required schema",
            )
        ):
            return IngestFailureKind.CONTENT
        if _is_transient_provider_failure(chain):
            return IngestFailureKind.TRANSIENT
        return IngestFailureKind.INFRASTRUCTURE

    if any(isinstance(error, (TimeoutError, ConnectionError)) for error in chain):
        return IngestFailureKind.TRANSIENT
    if any(
        isinstance(error, (ConfigError, ModuleNotFoundError, sqlite3.OperationalError, OSError))
        for error in chain
    ):
        return IngestFailureKind.INFRASTRUCTURE
    return IngestFailureKind.CONTENT


def _is_transient_provider_failure(chain: list[BaseException]) -> bool:
    transient_names = (
        "apiconnectionerror",
        "apitimeouterror",
        "connectionerror",
        "internalservererror",
        "ratelimiterror",
        "serviceunavailable",
        "timeout",
    )
    for error in chain:
        if any(marker in type(error).__name__.lower() for marker in transient_names):
            return True
        status_code = getattr(error, "status_code", None)
        if isinstance(status_code, int) and (
            status_code in {408, 409, 425, 429} or status_code >= 500
        ):
            return True
    return False


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return chain


def _batch_ingest_error(
    item: IngestItem,
    exc: Exception,
    failure_kind: IngestFailureKind,
) -> IngestError:
    label = (
        "temporary provider" if failure_kind is IngestFailureKind.TRANSIENT else "infrastructure"
    )
    return IngestError(
        f"Ingest stopped on {label} failure while processing {item.source_ref}: {exc}. "
        "The source item remains pending; no ingest-error review was created for it."
    )


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
        raw = path.read_bytes()
        read_error = None
        try:
            text, provenance = split_provenance(raw.decode("utf-8-sig"))
        except (UnicodeError, ValueError, BrainError) as exc:
            text, provenance = "", {}
            read_error = f"Source text or metadata is invalid ({type(exc).__name__})"
        source_ref = path.relative_to(paths.root).as_posix()
        event = Event(
            id=str(ulid.ULID()),
            timestamp=_now_utc(),
            kind=EventKind.LAUNDRY_INGESTED,
            source_ref=source_ref,
            raw_payload_path=source_ref,
            metadata=provenance,
        )
        items.append(
            IngestItem(
                source="laundry",
                source_ref=source_ref,
                text=text,
                event=event,
                laundry_path=path,
                raw_hash=hashlib.sha256(raw).hexdigest(),
                read_error=read_error,
            )
        )
    return items


def _is_unprocessed_laundry_file(paths: BrainPaths, path: Path) -> bool:
    try:
        relative = path.relative_to(paths.laundry_dir)
    except ValueError:
        return False
    return not relative.parts or relative.parts[0] not in {
        paths.laundry_processed_dir.name,
        FAILED_LAUNDRY_DIR_NAME,
    }


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
        if event.kind in {EventKind.BULK_IMPORTED, EventKind.LAUNDRY_INGESTED}:
            continue
        if not _event_has_payload(event):
            if event_id is not None:
                raise IngestError(
                    f"Event is not ingestible because it has no raw payload: {event.id}"
                )
            continue
        text = _event_text(paths.root, event)
        key = _item_key(IngestItem("events", event.source_ref, text, event))
        if completed(conn, key) or (event_id is None and completed(conn, "failed:" + key)):
            continue
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
        if event.kind in {EventKind.BULK_IMPORTED, EventKind.LAUNDRY_INGESTED}:
            continue
        if not _event_has_payload(event):
            if event_id is not None:
                raise IngestError(
                    f"Event is not ingestible because it has no raw payload: {event.id}"
                )
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


def _advance_skipped_events(paths: BrainPaths, conn: sqlite3.Connection) -> None:
    """Advance only over the contiguous non-ingestible suffix after completed work."""
    cursor = _get_cursor(conn, "events")
    last_skipped = None
    for event in read_all(paths.events_jsonl):
        if cursor is not None and event.id <= cursor:
            continue
        if event.kind not in {EventKind.BULK_IMPORTED, EventKind.LAUNDRY_INGESTED} and _event_has_payload(event):
            break
        last_skipped = event.id
    if last_skipped is not None:
        _set_cursor(conn, "events", last_skipped)


def _event_text(root: Path, event: Event) -> str:
    if event.raw_payload:
        return event.raw_payload
    if event.raw_payload_path:
        path = Path(event.raw_payload_path)
        if not path.is_absolute():
            path = root / path
        return path.read_text(encoding="utf-8")
    raise IngestError(f"Event has no raw payload: {event.id}")


def _event_has_payload(event: Event) -> bool:
    return bool(event.raw_payload or event.raw_payload_path)


def _detect_item_signal(item: IngestItem) -> SignalExtraction:
    body, _ = split_provenance(item.text)
    extraction = detect_signal(
        body,
        hint={
            "source": item.source,
            "source_ref": item.source_ref,
            "source_event": item.event.id,
            **item.hints,
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
                "predicate": normalize_predicate(fact.predicate),
            }
        )
        for fact in extraction.facts
    ]
    procedures = [
        procedure.model_copy(
            update={
                "source_event": item.event.id,
                "source_ref": item.event.source_ref,
            }
        )
        for procedure in extraction.procedure_candidates
    ]
    return extraction.model_copy(update={"facts": facts, "procedure_candidates": procedures})


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
    extraction = _focus_extraction(conn, extraction, config.ingest.confidence_auto_reject)
    entity_map, unresolved = _resolve_entities(
        conn,
        extraction.entities,
        review_writer,
        config,
    )

    _write_tier_proposals(
        conn,
        config,
        sorted(set(entity_map.values())),
        review_writer,
    )

    for candidate in extraction.facts:
        if candidate.confidence < config.ingest.confidence_auto_reject:
            continue
        normalized = _normalize_candidate(
            conn=conn,
            candidate=candidate,
            item=item,
            timeline_summary=extraction.timeline_summary,
            suggested_page_type=extraction.suggested_page_type,
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
            suggested_page_type=extraction.suggested_page_type,
        )

    for procedure_candidate in extraction.procedure_candidates:
        _handle_procedure_candidate(
            paths=paths,
            config=config,
            item=item,
            candidate=procedure_candidate,
            review_writer=review_writer,
            report=report,
            result=result,
        )

    report.processed += 1
    return result


def _incidental_value(name: str) -> bool:
    """Recognize concrete artifact/version values, never infer a person's identity."""
    return bool(re.search(
        r"(?:[a-zA-Z]:[\\/]|https?://|[/\\][\w.-]+|"
        r"\.(?:md|txt|json|toml|ya?ml|py|ts|tsx|js|jsx|html|ps1|sh|pdf|docx?)\b|"
        r"\bv?\d+(?:\.\d+)+\b|[a-zA-Z]\d+\.\d+)", name
    ))


def _focus_extraction(conn, extraction: SignalExtraction, reject: float) -> SignalExtraction:
    # Unused mentions remain in the source/cache; only factual endpoints need identity work.
    facts = []
    referenced: set[str] = set()
    for candidate in extraction.facts:
        if candidate.confidence < reject:
            continue
        if (candidate.object_type is FactObjectType.ENTITY
                and _incidental_value(candidate.object)
                and not matching_entity_ids(conn, candidate.object)):
            candidate = candidate.model_copy(update={"object_type": FactObjectType.LITERAL})
        facts.append(candidate)
        referenced.add(normalize_name(candidate.subject))
        if candidate.object_type is FactObjectType.ENTITY:
            referenced.add(normalize_name(candidate.object))
    entities = [entity for entity in extraction.entities
                if normalize_name(entity.name) in referenced
                or _slug_from_name(entity.name) in referenced
                or bool(set(matching_entity_ids(conn, entity.name)) & referenced)]
    return extraction.model_copy(update={"entities": entities, "facts": facts})


def _resolve_entities(
    conn: sqlite3.Connection,
    signal_entities: list[SignalEntity],
    review_writer: ReviewWriter,
    config: Config,
) -> tuple[dict[str, str], set[str]]:
    entity_map: dict[str, str] = {}
    unresolved: set[str] = set()

    for signal_entity in _unique_signal_entities(signal_entities):
        if signal_entity.confidence < config.ingest.confidence_auto_reject:
            unresolved.add(signal_entity.name)
            continue
        entity = resolve_entity(conn, signal_entity.name, signal_entity.type,
                                confidence=signal_entity.confidence,
                                auto_accept=config.ingest.entity_confidence_auto_accept)
        if entity is None:
            unresolved.add(signal_entity.name)
            _write_new_entity_review(review_writer, signal_entity)
            continue
        entity_map[signal_entity.name] = entity.id
        entity_map[entity.title] = entity.id
        entity_map[entity.id] = entity.id

    return entity_map, unresolved


def _unique_signal_entities(signal_entities: list[SignalEntity]) -> list[SignalEntity]:
    from brain.pipeline.resolve import normalize_name
    unique: dict[str, SignalEntity] = {}
    for entity in signal_entities:
        key = normalize_name(entity.name)
        if key not in unique or unique[key].confidence < entity.confidence:
            unique[key] = entity
    return list(unique.values())


def _normalize_candidate(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
    item: IngestItem,
    timeline_summary: str,
    suggested_page_type: PageType | None,
    entity_map: dict[str, str],
    unresolved: set[str],
    review_writer: ReviewWriter,
) -> FactCandidate | None:
    unresolved_names: list[str] = []
    subject = _candidate_entity_id(
        conn=conn,
        name=candidate.subject,
        hint_type=None,
        entity_map=entity_map,
        unresolved=unresolved,
        review_writer=review_writer,
    )
    if subject is None:
        unresolved_names.append(candidate.subject)
        _write_pending_fact_review(
            review_writer,
            candidate,
            item,
            timeline_summary,
            suggested_page_type,
            unresolved_names,
        )
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
            unresolved_names.append(candidate.object)
            _write_pending_fact_review(
                review_writer,
                candidate,
                item,
                timeline_summary,
                suggested_page_type,
                unresolved_names,
            )
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

    entity = resolve_entity(conn, name, hint_type, allow_create=False)
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
    suggested_page_type: PageType | None = None,
    confidence_approved: bool = False,
) -> None:
    auto_accept = config.ingest.confidence_auto_accept
    auto_reject = config.ingest.confidence_auto_reject

    decision = classify_fact(conn, candidate, config)
    if decision is Decision.NOOP:
        return
    if not confidence_approved and candidate.confidence < auto_reject:
        return
    if not confidence_approved and candidate.confidence < auto_accept:
        _write_low_confidence_review(review_writer, candidate)
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
        timeline_summary=fact_sentence(candidate.subject, candidate.predicate, candidate.object,
                                       chinese=uses_chinese(item.text, config.ingest.output_language)),
        report=report,
        result=result,
        suggested_page_type=suggested_page_type,
        output_language=config.ingest.output_language,
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
    suggested_page_type: PageType | None = None,
    *,
    force_page: bool = False,
    output_language: str = "source",
) -> None:
    entity = get_entity(conn, subject_id)
    if entity is None:
        raise IngestError(f"Cannot touch page for missing entity: {subject_id}")

    page_type = _page_type_for_entity(entity, suggested_page_type)
    page_path = _page_path_for_entity(paths, entity, page_type)
    _persist_entity_page_path(conn, paths, entity, page_path)
    if not page_path.exists():
        if not force_page and _should_delay_stub_page(entity):
            return
        _write_stub_page(page_path, entity, source_ref, page_type)
    else:
        page = parse_page(page_path)
        if source_ref not in page.sources:
            update_sources(page_path, [*page.sources, source_ref])

    timeline_key = (entity.id, event_id)
    if timeline_key not in result.timeline_written:
        existing_page = parse_page(page_path)
        if not any(f"[event:{event_id}]" in line for line in existing_page.timeline):
            append_timeline(
                page_path,
                TimelineEntry(
                    date=event_date,
                    event_id=event_id,
                    description=timeline_summary,
                ),
            )
        result.timeline_written.add(timeline_key)

    # Only machine-owned summaries can refresh automatically.
    from brain.pipeline.summaries import refresh_generated_summary
    refresh_generated_summary(conn, page_path, entity.id, output_language=output_language)

    relative = page_path.relative_to(paths.root).as_posix()
    _append_unique(report.pages_touched, relative)
    result.page_paths.add(page_path)
    result.page_slugs.add(entity.id)


def _should_delay_stub_page(entity: Entity) -> bool:
    return (
        _is_transient_entity(entity) and entity.mention_count < TRANSIENT_ENTITY_PAGE_MIN_MENTIONS
    )


def _is_transient_entity(entity: Entity) -> bool:
    slug = entity.id.lower()
    title = entity.title.lower()
    if any(pattern.search(slug) or pattern.search(title) for pattern in TRANSIENT_ENTITY_PATTERNS):
        return True

    terms = {part for part in slug.split("-") if part}
    return bool(terms & TRANSIENT_ENTITY_TERMS)


def _page_type_for_entity(entity: Entity, suggested_page_type: PageType | None = None) -> PageType:
    if entity.type is EntityType.PROJECT:
        return PageType.PROJECT
    if entity.type is EntityType.CONCEPT:
        return PageType.CONCEPT
    if entity.type is EntityType.EVENT:
        return PageType.EVENT
    if suggested_page_type is PageType.EXPERIENCE:
        return PageType.EXPERIENCE
    return PageType.ENTITY


def _page_path_for_entity(paths: BrainPaths, entity: Entity, page_type: PageType) -> Path:
    if entity.page_path:
        configured = Path(entity.page_path)
        candidate = configured if configured.is_absolute() else paths.root / configured
        if candidate.exists():
            return candidate

    return _page_dir(paths, page_type) / f"{entity.id}.md"


def _page_dir(paths: BrainPaths, page_type: PageType) -> Path:
    if page_type is PageType.PROJECT:
        return paths.projects_dir
    if page_type is PageType.CONCEPT:
        return paths.concepts_dir
    if page_type is PageType.EVENT:
        return paths.events_dir
    if page_type is PageType.EXPERIENCE:
        return paths.experiences_dir
    if page_type is PageType.CONVERSATION:
        return paths.conversations_dir
    return paths.entities_dir


def _persist_entity_page_path(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    entity: Entity,
    page_path: Path,
) -> None:
    relative = page_path.relative_to(paths.root).as_posix()
    if entity.page_path == relative:
        return
    conn.execute("UPDATE entities SET page_path = ? WHERE id = ?", (relative, entity.id))


def _write_stub_page(
    path: Path,
    entity: Entity,
    source_ref: str,
    page_type: PageType,
) -> None:
    now = _now_utc()
    page = Page(
        frontmatter=Frontmatter(
            type=page_type,
            slug=entity.id,
            title=entity.title,
            tier=entity.tier if page_type is PageType.ENTITY else None,
            created=now,
            updated=now,
            tags=[],
            aliases=[],
            external_ids={},
            entity_type=entity.type.value,
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


def _write_pending_fact_review(
    review_writer: ReviewWriter,
    candidate: FactCandidate,
    item: IngestItem,
    timeline_summary: str,
    suggested_page_type: PageType | None,
    unresolved_entities: list[str],
) -> None:
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "event": _review_event(item),
        "timeline_summary": timeline_summary,
        "suggested_page_type": _enum_value(suggested_page_type),
        "unresolved_entities": unresolved_entities,
    }
    body = "\n".join(
        [
            "# Pending fact",
            "",
            "This fact depends on unresolved entities. Resolve the entity review items first, then approve this pending fact.",
            "",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "```",
        ]
    )
    review_writer.write("pending_fact", body)


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


def _write_ingest_error_review(
    review_writer: ReviewWriter,
    item: IngestItem,
    exc: Exception,
    *,
    failure_kind: IngestFailureKind,
) -> None:
    body = "\n".join(
        [
            "# Ingest error",
            "",
            f"- source: {item.source}",
            f"- source_ref: {item.source_ref}",
            f"- event_id: {item.event.id}",
            f"- event_kind: {item.event.kind.value}",
            f"- failure_kind: {failure_kind.value}",
            f"- error_type: {type(exc).__name__}",
            f"- error: {exc}",
            "",
            "## Event",
            "",
            "```json",
            json.dumps(_review_event(item), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Traceback",
            "",
            "```text",
            type(exc).__name__ + ": " + str(exc)[:300],
            "```",
        ]
    )
    review_writer.write("ingest_error", body)


def _handle_procedure_candidate(
    paths: BrainPaths,
    config: Config,
    item: IngestItem,
    candidate: ProcedureCandidate,
    review_writer: ReviewWriter,
    report: IngestReport,
    result: ItemResult,
) -> None:
    auto_accept = config.ingest.confidence_auto_accept
    auto_reject = config.ingest.confidence_auto_reject

    if candidate.confidence < auto_reject:
        return
    if _procedure_already_known(paths, candidate):
        return
    validation_error = _procedure_candidate_error(paths, candidate)
    if validation_error is not None:
        _write_procedure_candidate_review(review_writer, candidate, item, reason=validation_error)
        return

    reason = "candidate"
    if candidate.confidence < auto_accept:
        reason = "low_confidence"
    _write_procedure_candidate_review(review_writer, candidate, item, reason=reason)


def _procedure_candidate_error(paths: BrainPaths, candidate: ProcedureCandidate) -> str | None:
    if not SLUG_PATTERN.fullmatch(candidate.slug):
        return "invalid_slug"
    if (paths.procedures_dir / f"{candidate.slug}.md").exists():
        return "procedure_exists"
    return None


def _write_procedure_candidate_review(
    review_writer: ReviewWriter,
    candidate: ProcedureCandidate,
    item: IngestItem,
    *,
    reason: str,
) -> None:
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "event": _review_event(item),
        "reason": reason,
    }
    body = "\n".join(
        [
            "# Procedure candidate",
            "",
            f"- reason: {reason}",
            "",
            "Review the reusable procedure candidate and create or merge a procedure page manually.",
            "",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "```",
        ]
    )
    review_writer.write("procedure_candidate", body)


def _record_item_success(
    paths: BrainPaths,
    conn: sqlite3.Connection,
    item: IngestItem,
    result: ItemResult,
    report: IngestReport,
    *,
    advance_cursor: bool = True,
) -> None:
    if item.source == "laundry":
        event = item.event.model_copy(
            update={
                "extracted_facts": result.fact_ids,
                "affected_pages": sorted(result.page_slugs),
                "raw_payload": None,
                "raw_payload_path": item.source_ref,
            }
        )
        append_event(paths.events_jsonl, event)
        if item.laundry_path is None:
            raise IngestError("Laundry item is missing its source path")
        _move_protected(item.laundry_path, paths.root / item.source_ref)
        report.laundry_archived += 1
        _set_cursor(conn, "laundry", item.source_ref)
        return

    if advance_cursor:
        _set_cursor(conn, "events", item.event.id)


def _archive_laundry_item(paths: BrainPaths, path: Path) -> None:
    paths.laundry_processed_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_processed_path(paths.laundry_processed_dir / path.name)
    shutil.move(str(path), str(target))


def _archive_failed_laundry_item(paths: BrainPaths, path: Path) -> None:
    target_dir = paths.laundry_dir / FAILED_LAUNDRY_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_processed_path(target_dir / path.name)
    _move_protected(path, target)


def _move_protected(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    protect_path(source, None)
    protect_path(target, source.read_bytes())
    source.replace(target)


def _review_event(item: IngestItem) -> dict:
    return item.event.model_copy(update={"raw_payload": None, "raw_payload_path": item.source_ref}).model_dump(mode="json")


def _procedure_already_known(paths: BrainPaths, candidate: ProcedureCandidate) -> bool:
    from brain.pipeline.resolve import normalize_name
    key = normalize_name(candidate.title)
    for path in paths.procedures_dir.glob("*.md"):
        page = parse_page(path)
        if page.frontmatter.slug == candidate.slug or normalize_name(page.frontmatter.title) == key:
            return True
    for path in paths.review_dir.glob("*_procedure_candidate.md"):
        from brain.pipeline.review import parse_review_file
        parsed = parse_review_file(path).data.get("candidate", {})
        if parsed.get("suggested_slug") == candidate.slug or normalize_name(parsed.get("title", "")) == key:
            return True
    return False


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
    auto_reindex: bool | None = None,
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

    should_reindex = config.import_.auto_reindex if auto_reindex is None else auto_reindex
    if should_reindex and report.pages_touched:
        _run_auto_reindex(paths, report)

    should_commit = config.git.auto_commit if auto_commit is None else auto_commit
    if should_commit:
        _checkpoint_db(conn)
        from brain import git_ops

        git_ops.commit(
            paths.root,
            f"ingest: process {report.processed} items",
            paths=_commit_paths(paths),
        )


def _run_auto_reindex(paths: BrainPaths, report: IngestReport) -> None:
    try:
        slugs = _touched_page_slugs(paths, report.pages_touched)
        if not slugs:
            return
        reindex_report = reindex(paths.root, page_filter=slugs, no_commit=True)
    except Exception as exc:  # keep ingest success independent from embedding availability
        report.errors.append(f"auto-reindex failed: {exc}")
        return

    for error in reindex_report.errors:
        report.errors.append(f"auto-reindex: {error}")


def _touched_page_slugs(paths: BrainPaths, touched_pages: list[str]) -> list[str]:
    slugs: list[str] = []
    for relative in touched_pages:
        page = parse_page(paths.root / relative)
        _append_unique(slugs, page.frontmatter.slug)
    return slugs


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

    conn = connect_readonly(path)
    conn.row_factory = sqlite3.Row
    return conn


def _close_ingest_connection(conn: sqlite3.Connection, db_path: Path) -> None:
    try:
        _checkpoint_db(conn)
    finally:
        conn.close()


def _checkpoint_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


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


def _next_review_seq(review_dir: Path, date: str) -> int:
    if not review_dir.exists():
        return 1

    max_seq = 0
    for path in review_dir.rglob(f"{date}_*_*.md"):
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
    atomic_text(path, normalized)


def _with_decision_section(body: str) -> str:
    stripped = body.strip()
    if "## Decision" in stripped:
        return f"{stripped}\n"
    return f"{stripped}\n\n{REVIEW_DECISION_SECTION}"
