from __future__ import annotations

import importlib
import json
import re
import shutil
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import frontmatter
import ulid
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from brain.config import load_config
from brain.db.connection import connect
from brain.db.entities import add_alias, get_entity, lookup_by_alias, upsert_entity
from brain.db.facts import add_fact, find_active_facts, supersede
from brain.db.tier import record_tier_decision
from brain.exceptions import BrainError
from brain.git_ops import commit
from brain.ledger import append_event
from brain.llm import client as llm_client
from brain.models import (
    Entity,
    EntityAliasSource,
    EntityType,
    Event,
    EventKind,
    Fact,
    FactCandidate,
    Frontmatter,
    Page,
    PageType,
    ProcedureStatus,
    Tier,
)
from brain.models.page import SLUG_PATTERN
from brain.pages import parse_page, write_page
from brain.pages.timeline import TimelineEntry, format_entry, parse_entry
from brain.paths import BrainPaths


class ReviewKind(StrEnum):
    """Supported review item kinds."""

    FACT_CONFLICT = "fact_conflict"
    INGEST_ERROR = "ingest_error"
    LOW_CONFIDENCE_FACT = "low_confidence_fact"
    PENDING_FACT = "pending_fact"
    PROCEDURE_CANDIDATE = "procedure_candidate"
    TIER_PROPOSAL = "tier_proposal"
    LINT_FINDING = "lint_finding"
    NEW_ENTITY_REVIEW = "new_entity_review"


class ReviewAction(StrEnum):
    """User-selected action in a review file."""

    APPROVE = "approve"
    REJECT = "reject"
    DEFER = "defer"
    NONE = "none"


class ReviewStatus(StrEnum):
    """Lifecycle status stored in review file frontmatter."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class ReviewItem(BaseModel):
    """Pending review queue item summary."""

    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(..., min_length=1)
    kind: ReviewKind
    status: ReviewStatus = ReviewStatus.PENDING
    path: Path
    created: datetime | None = None


class ReviewDecision(BaseModel):
    """Parsed review file including action and structured payload."""

    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(..., min_length=1)
    kind: ReviewKind
    status: ReviewStatus = ReviewStatus.PENDING
    path: Path
    action: ReviewAction = ReviewAction.NONE
    candidate: FactCandidate | None = None
    active_facts: list[Fact] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    @property
    def decision(self) -> ReviewAction:
        """Alias for callers that use decision terminology."""
        return self.action


class ReviewApplyReport(BaseModel):
    """Result of applying one review decision."""

    model_config = ConfigDict(extra="forbid")

    review_id: str
    kind: ReviewKind
    action: ReviewAction
    path: Path
    applied: bool = False
    skipped: bool = False
    archived_path: Path | None = None
    facts_added: list[int] = Field(default_factory=list)
    facts_superseded: list[int] = Field(default_factory=list)
    tier_proposal_id: int | None = None
    entity_id: str | None = None
    pages_touched: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ReviewBatchReport(BaseModel):
    """Aggregate result for applying pending review decisions."""

    model_config = ConfigDict(extra="forbid")

    applied: int = 0
    approved: int = 0
    rejected: int = 0
    deferred: int = 0
    archived: int = 0
    skipped: int = 0
    reports: list[ReviewApplyReport] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.IGNORECASE | re.DOTALL)
CHECKBOX_RE = re.compile(r"^\s*(?:[-*]\s*)?\[(?P<mark>[xX])\]\s*(?P<label>.+?)\s*$")
KEY_VALUE_RE = re.compile(r"^\s*(?:[-*]\s*)?(?P<key>[A-Za-z_][\w-]*):\s*(?P<value>.*?)\s*$")
REVIEW_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[^_]+_(?P<kind>.+)$")


def list_pending(brain_root: Path, kind: str | ReviewKind | None = None) -> list[ReviewItem]:
    """List direct pending markdown review files under brain_root/review."""
    paths = BrainPaths(Path(brain_root))
    if not paths.review_dir.exists():
        return []

    kind_filter = _coerce_kind(kind) if kind is not None else None
    items: list[ReviewItem] = []
    for path in sorted(paths.review_dir.glob("*.md")):
        item = _read_review_item(path)
        if item.status is not ReviewStatus.PENDING:
            continue
        if kind_filter is not None and item.kind is not kind_filter:
            continue
        items.append(item)
    return items


def resolve_review_path(brain_root: Path, review_id: str) -> Path:
    """Resolve a full review id, file name, or unique id prefix to a pending file."""
    paths = BrainPaths(Path(brain_root))
    query = review_id[:-3] if review_id.endswith(".md") else review_id
    candidates = sorted(paths.review_dir.glob("*.md"))

    exact = [path for path in candidates if path.stem == query or path.name == review_id]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise BrainError(f"Ambiguous review id: {review_id}")

    matches = [path for path in candidates if path.stem.startswith(query)]
    if not matches:
        raise BrainError(f"Review item not found: {review_id}")
    if len(matches) > 1:
        joined = ", ".join(path.stem for path in matches)
        raise BrainError(f"Ambiguous review id: {review_id} ({joined})")
    return matches[0]


def parse_review_file(path: Path) -> ReviewDecision:
    """Parse frontmatter, JSON-fence payloads, and selected review action."""
    review_path = Path(path)
    post = frontmatter.loads(review_path.read_text(encoding="utf-8"))
    metadata = dict(post.metadata)
    content = post.content

    review_id = str(metadata.get("review_id") or review_path.stem)
    kind = _coerce_kind(metadata.get("kind") or _kind_from_review_id(review_id))
    action, action_errors = _selected_action(content)
    data, data_errors = _extract_review_data(kind, content)
    if kind in {
        ReviewKind.FACT_CONFLICT,
        ReviewKind.LOW_CONFIDENCE_FACT,
        ReviewKind.PENDING_FACT,
    }:
        candidate, candidate_errors = _candidate_from_data(data)
        active_facts, fact_errors = _active_facts_from_data(data)
    else:
        candidate = None
        active_facts = []
        candidate_errors = []
        fact_errors = []

    return ReviewDecision(
        review_id=review_id,
        kind=kind,
        status=_coerce_status(metadata.get("status")),
        path=review_path,
        action=action,
        candidate=candidate,
        active_facts=active_facts,
        data=data,
        errors=[*action_errors, *data_errors, *candidate_errors, *fact_errors],
    )


def apply_pending(brain_root: Path, kind: str | ReviewKind | None = None) -> ReviewBatchReport:
    """Apply all pending review files that have a selected decision."""
    paths = BrainPaths(Path(brain_root))
    kind_filter = _coerce_kind(kind) if kind is not None else None
    batch = ReviewBatchReport()
    conn = connect(paths.db_path)
    try:
        for path in sorted(paths.review_dir.glob("*.md")):
            try:
                item = _read_review_item(path)
            except Exception as exc:
                _add_batch_report(batch, _failed_review_report(path, exc))
                continue
            if item.status is not ReviewStatus.PENDING:
                continue
            if kind_filter is not None and item.kind is not kind_filter:
                continue

            try:
                report = apply_decision(conn, parse_review_file(item.path))
            except Exception as exc:
                report = _failed_review_report(item.path, exc, item=item)
            _add_batch_report(batch, report)
    finally:
        _checkpoint_and_close(conn)
        _remove_sqlite_sidecars(paths.db_path)

    if batch.applied > 0:
        _auto_commit(paths, batch.applied)
    return batch


def _add_batch_report(batch: ReviewBatchReport, report: ReviewApplyReport) -> None:
    batch.reports.append(report)
    batch.errors.extend(report.errors)
    batch.follow_ups.extend(report.follow_ups)

    if report.applied:
        batch.applied += 1
        if report.action is ReviewAction.APPROVE:
            batch.approved += 1
        elif report.action is ReviewAction.REJECT:
            batch.rejected += 1
        if report.archived_path is not None:
            batch.archived += 1
    elif report.action is ReviewAction.DEFER:
        batch.deferred += 1

    if report.skipped or report.errors:
        batch.skipped += 1


def _failed_review_report(
    path: Path,
    exc: Exception,
    *,
    item: ReviewItem | None = None,
) -> ReviewApplyReport:
    report = ReviewApplyReport(
        review_id=item.review_id if item is not None else Path(path).stem,
        kind=item.kind if item is not None else _safe_kind_from_path(path),
        action=ReviewAction.NONE,
        path=Path(path),
        skipped=True,
    )
    report.errors.append(f"{Path(path).name}: {exc}")
    return report


def _safe_kind_from_path(path: Path) -> ReviewKind:
    try:
        return _coerce_kind(_kind_from_review_id(Path(path).stem))
    except Exception:
        return ReviewKind.INGEST_ERROR


def apply_decision(conn: sqlite3.Connection, decision: ReviewDecision) -> ReviewApplyReport:
    """Apply one parsed review decision, inferring brain root from decision.path."""
    report = ReviewApplyReport(
        review_id=decision.review_id,
        kind=decision.kind,
        action=decision.action,
        path=decision.path,
    )

    if decision.errors:
        report.errors.extend(decision.errors)
        report.skipped = True
        return report
    if decision.action in {ReviewAction.NONE, ReviewAction.DEFER}:
        report.skipped = True
        return report

    paths = BrainPaths(_infer_brain_root(decision.path))
    if decision.action is ReviewAction.REJECT:
        return _reject_decision(conn, paths, decision, report)
    if decision.kind is ReviewKind.FACT_CONFLICT:
        return _approve_fact_conflict(conn, paths, decision, report)
    if decision.kind is ReviewKind.LOW_CONFIDENCE_FACT:
        return _approve_low_confidence_fact(conn, paths, decision, report)
    if decision.kind is ReviewKind.PENDING_FACT:
        return _approve_pending_fact(conn, paths, decision, report)
    if decision.kind is ReviewKind.TIER_PROPOSAL:
        return _approve_tier_proposal(conn, paths, decision, report)
    if decision.kind is ReviewKind.NEW_ENTITY_REVIEW:
        return _approve_new_entity_review(conn, paths, decision, report)
    if decision.kind is ReviewKind.PROCEDURE_CANDIDATE:
        return _approve_procedure_candidate(conn, paths, decision, report)

    report.errors.append(f"Approve is not implemented for review kind: {decision.kind.value}")
    report.skipped = True
    return report


def _read_review_item(path: Path) -> ReviewItem:
    post = frontmatter.loads(path.read_text(encoding="utf-8"))
    metadata = dict(post.metadata)
    review_id = str(metadata.get("review_id") or path.stem)
    return ReviewItem(
        review_id=review_id,
        kind=_coerce_kind(metadata.get("kind") or _kind_from_review_id(review_id)),
        status=_coerce_status(metadata.get("status")),
        path=path,
        created=_coerce_datetime(metadata.get("created")),
    )


def _reject_decision(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    decision: ReviewDecision,
    report: ReviewApplyReport,
) -> ReviewApplyReport:
    with conn:
        if decision.kind is ReviewKind.TIER_PROPOSAL:
            proposal = _find_tier_proposal(conn, paths, decision)
            if proposal is not None:
                record_tier_decision(conn, int(proposal["id"]), ReviewStatus.REJECTED.value)
                report.tier_proposal_id = int(proposal["id"])
                report.entity_id = str(proposal["entity_id"])
        _record_review_event(paths, decision, "rejected", report)

    report.archived_path = _mark_and_archive(decision.path, ReviewStatus.REJECTED, decision.action)
    report.applied = True
    return report


def _approve_fact_conflict(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    decision: ReviewDecision,
    report: ReviewApplyReport,
) -> ReviewApplyReport:
    if decision.candidate is None:
        report.errors.append(f"Review file has no fact candidate: {decision.path}")
        report.skipped = True
        return report

    candidate = decision.candidate
    now = _now_utc()
    valid_to = candidate.valid_from or now.date().isoformat()

    with conn:
        old_active = find_active_facts(conn, candidate.subject, candidate.predicate)
        new_fact_id = add_fact(conn, _fact_from_candidate(candidate, now))
        report.facts_added.append(new_fact_id)
        report.entity_id = candidate.subject

        for old_fact in old_active:
            if old_fact.id is None:
                continue
            supersede(conn, old_fact.id, new_fact_id)
            conn.execute("UPDATE facts SET valid_to = ? WHERE id = ?", (valid_to, old_fact.id))
            report.facts_superseded.append(old_fact.id)

        _record_review_event(paths, decision, "approved", report)

    report.archived_path = _mark_and_archive(decision.path, ReviewStatus.APPROVED, decision.action)
    report.applied = True
    return report


def _approve_low_confidence_fact(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    decision: ReviewDecision,
    report: ReviewApplyReport,
) -> ReviewApplyReport:
    if decision.candidate is None:
        report.errors.append(f"Review file has no fact candidate: {decision.path}")
        report.skipped = True
        return report

    with conn:
        fact_id = add_fact(conn, _fact_from_candidate(decision.candidate, _now_utc()))
        report.facts_added.append(fact_id)
        report.entity_id = decision.candidate.subject
        _record_review_event(paths, decision, "approved", report)

    report.archived_path = _mark_and_archive(decision.path, ReviewStatus.APPROVED, decision.action)
    report.applied = True
    return report


def _approve_pending_fact(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    decision: ReviewDecision,
    report: ReviewApplyReport,
) -> ReviewApplyReport:
    if decision.candidate is None:
        report.errors.append(f"Review file has no pending fact candidate: {decision.path}")
        report.skipped = True
        return report

    normalized = _resolve_pending_candidate(conn, decision.candidate)
    if normalized is None:
        report.errors.append("Pending fact still has unresolved entity references")
        report.skipped = True
        return report

    event = _pending_event(decision, normalized)
    timeline_summary = _string_data(decision, "timeline_summary") or "Approved pending fact."
    suggested_page_type = _page_type_data(decision)

    ingest_pipeline = importlib.import_module("brain.pipeline.ingest")

    ingest_report = ingest_pipeline.IngestReport()
    review_writer = ingest_pipeline.ReviewWriter.create(paths, ingest_report)
    item = ingest_pipeline.IngestItem(
        source="events",
        source_ref=normalized.source_ref or event.source_ref,
        text="",
        event=event,
    )
    result = ingest_pipeline.ItemResult()

    with conn:
        ingest_pipeline._handle_candidate(
            conn=conn,
            paths=paths,
            config=load_config(paths.config_path),
            item=item,
            candidate=normalized,
            timeline_summary=timeline_summary,
            review_writer=review_writer,
            report=ingest_report,
            result=result,
            suggested_page_type=suggested_page_type,
        )
        ingest_pipeline._rebuild_touched_backlinks(conn, paths, ingest_report.pages_touched)

        for fact_id in result.fact_ids:
            report.facts_added.append(int(fact_id))
        report.pages_touched.extend(ingest_report.pages_touched)
        report.entity_id = normalized.subject
        report.follow_ups.extend(
            f"Pending fact produced review item: {path}" for path in ingest_report.review_files
        )
        _record_review_event(paths, decision, "approved", report)

    from brain.pages import append_log, regenerate_index

    regenerate_index(paths.root)
    append_log(
        paths.root,
        f"- {_now_utc().strftime('%Y-%m-%d %H:%M')} review: applied pending fact {decision.review_id}",
    )

    report.archived_path = _mark_and_archive(decision.path, ReviewStatus.APPROVED, decision.action)
    report.applied = True
    return report


def _approve_new_entity_review(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    decision: ReviewDecision,
    report: ReviewApplyReport,
) -> ReviewApplyReport:
    name = _string_data(decision, "name")
    if name is None:
        report.errors.append("New entity review is missing name")
        report.skipped = True
        return report

    merge_into = _string_data(decision, "merge_into") or _string_data(decision, "entity_id")
    slug = _string_data(decision, "slug")
    if merge_into is None and slug is None:
        report.errors.append("Approve new_entity_review requires slug or merge_into")
        report.skipped = True
        return report

    now = _now_utc()
    with conn:
        if merge_into is not None:
            entity = get_entity(conn, merge_into)
            if entity is None:
                report.errors.append(f"Target entity does not exist: {merge_into}")
                report.skipped = True
                return report
            _add_alias_if_missing(conn, name, entity.id)
            report.entity_id = entity.id
        else:
            assert slug is not None
            if not _valid_slug(slug):
                report.errors.append(f"Invalid entity slug: {slug}")
                report.skipped = True
                return report
            entity = get_entity(conn, slug)
            if entity is None:
                entity_type = _entity_type_data(decision) or EntityType.CONCEPT
                entity = Entity(
                    id=slug,
                    type=entity_type,
                    title=name,
                    page_path=_default_page_path(slug, entity_type),
                    tier=Tier.TIER_3,
                    mention_count=1,
                    first_seen=now,
                    last_seen=now,
                    metadata={},
                )
                upsert_entity(conn, entity)
            _add_alias_if_missing(conn, name, entity.id)
            report.entity_id = entity.id

        _record_review_event(paths, decision, "approved", report)

    report.archived_path = _mark_and_archive(decision.path, ReviewStatus.APPROVED, decision.action)
    report.applied = True
    return report


def _approve_procedure_candidate(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    decision: ReviewDecision,
    report: ReviewApplyReport,
) -> ReviewApplyReport:
    candidate = decision.data.get("candidate")
    if not isinstance(candidate, dict):
        report.errors.append("Procedure candidate review is missing candidate payload")
        report.skipped = True
        return report

    slug = str(candidate.get("suggested_slug") or candidate.get("slug") or "").strip()
    title = str(candidate.get("title") or "").strip()
    summary = str(candidate.get("summary") or "").strip()
    if not SLUG_PATTERN.fullmatch(slug):
        report.errors.append(f"Invalid procedure slug: {slug}")
        report.skipped = True
        return report
    if not title or not summary:
        report.errors.append("Procedure candidate requires title and summary")
        report.skipped = True
        return report

    path = paths.procedures_dir / f"{slug}.md"
    if path.exists():
        report.errors.append(f"Procedure already exists: {slug}")
        report.skipped = True
        return report

    event = _procedure_candidate_event(decision)
    steps = [str(step).strip() for step in candidate.get("steps", []) if str(step).strip()]
    now = _now_utc()
    page = Page(
        frontmatter=Frontmatter(
            type=PageType.PROCEDURE,
            slug=slug,
            title=title,
            created=now,
            updated=now,
            tags=[],
            aliases=[],
            external_ids={},
            status=ProcedureStatus.RAW,
            success_count=0,
            fail_count=0,
        ),
        compiled_truth=_procedure_candidate_truth(summary, steps),
        timeline=[
            format_entry(
                TimelineEntry(
                    date=event.timestamp.date().isoformat(),
                    event_id=event.id,
                    description=f"Procedure candidate approved from {event.source_ref}.",
                )
            )
        ],
        sources=[event.source_ref],
    )
    write_page(path, page)
    report.entity_id = slug
    report.pages_touched.append(path.relative_to(paths.root).as_posix())

    with conn:
        _record_review_event(paths, decision, "approved", report)

    from brain.pages import append_log, regenerate_index

    regenerate_index(paths.root)
    append_log(
        paths.root,
        f"- {_now_utc().strftime('%Y-%m-%d %H:%M')} review: created procedure {slug}",
    )

    report.archived_path = _mark_and_archive(decision.path, ReviewStatus.APPROVED, decision.action)
    report.applied = True
    return report


def _approve_tier_proposal(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    decision: ReviewDecision,
    report: ReviewApplyReport,
) -> ReviewApplyReport:
    proposal = _find_tier_proposal(conn, paths, decision)
    if proposal is None:
        report.errors.append(f"Tier proposal not found for review: {decision.review_id}")
        report.skipped = True
        return report

    entity_id = str(proposal["entity_id"])
    proposed_tier = Tier(int(proposal["proposed_tier"]))
    page_path = _entity_page_path(conn, paths, entity_id)
    if page_path is None:
        report.errors.append(f"Entity page not found for tier proposal: {entity_id}")
        report.skipped = True
        return report

    page = parse_page(page_path)
    timeline = [parse_entry(line) for line in page.timeline]
    compiled_truth = llm_client.rewrite_compiled_truth(timeline, page.compiled_truth)
    report.pages_touched.append(page_path.relative_to(paths.root).as_posix())

    with conn:
        conn.execute("UPDATE entities SET tier = ? WHERE id = ?", (int(proposed_tier), entity_id))
        record_tier_decision(conn, int(proposal["id"]), ReviewStatus.APPROVED.value)
        report.tier_proposal_id = int(proposal["id"])
        report.entity_id = entity_id
        _record_review_event(paths, decision, "approved", report)

    updated_frontmatter = page.frontmatter.model_copy(
        update={"tier": proposed_tier, "updated": _now_utc()}
    )
    write_page(
        page_path,
        page.model_copy(
            update={"frontmatter": updated_frontmatter, "compiled_truth": compiled_truth}
        ),
    )
    report.archived_path = _mark_and_archive(decision.path, ReviewStatus.APPROVED, decision.action)
    report.applied = True
    return report


def _fact_from_candidate(candidate: FactCandidate, asserted_at: datetime) -> Fact:
    return Fact(
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


def _find_tier_proposal(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    decision: ReviewDecision,
) -> sqlite3.Row | None:
    review_refs = _review_file_refs(paths, Path(decision.path))
    placeholders = ",".join("?" for _ in review_refs)
    row = conn.execute(
        f"""
        SELECT *
        FROM tier_proposals
        WHERE review_file IN ({placeholders})
          AND decision IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        review_refs,
    ).fetchone()
    if row is not None:
        return row

    entity_id = _string_data(decision, "entity_id")
    proposed_tier = _int_data(decision, "proposed_tier")
    if entity_id is None or proposed_tier is None:
        return None

    return conn.execute(
        """
        SELECT *
        FROM tier_proposals
        WHERE entity_id = ?
          AND proposed_tier = ?
          AND decision IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (entity_id, proposed_tier),
    ).fetchone()


def _entity_page_path(conn: sqlite3.Connection, paths: BrainPaths, entity_id: str) -> Path | None:
    entity = get_entity(conn, entity_id)
    if entity is None:
        return None

    candidates: list[Path] = []
    if entity.page_path:
        page_path = Path(entity.page_path)
        candidates.append(page_path if page_path.is_absolute() else paths.root / page_path)
    candidates.append(paths.entities_dir / f"{entity.id}.md")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _record_review_event(
    paths: BrainPaths,
    decision: ReviewDecision,
    status: str,
    report: ReviewApplyReport,
) -> None:
    append_event(
        paths.events_jsonl,
        Event(
            id=str(ulid.ULID()),
            timestamp=_now_utc(),
            kind=EventKind.REVIEW_DECIDED,
            source_ref=_review_source_ref(paths, decision.path),
            extracted_facts=[str(fact_id) for fact_id in report.facts_added],
            affected_pages=list(report.pages_touched),
            metadata={
                "review_id": decision.review_id,
                "kind": decision.kind.value,
                "decision": decision.action.value,
                "status": status,
                "tier_proposal_id": report.tier_proposal_id,
                "entity_id": report.entity_id,
                "facts_superseded": report.facts_superseded,
            },
        ),
    )


def _mark_and_archive(path: Path, status: ReviewStatus, action: ReviewAction) -> Path:
    review_path = Path(path)
    _mark_review_file(review_path, status, action)

    archive_dir = _find_review_dir(review_path) / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_archive_path(archive_dir / review_path.name)
    shutil.move(str(review_path), str(target))
    return target


def _mark_review_file(path: Path, status: ReviewStatus, action: ReviewAction) -> None:
    post = frontmatter.loads(path.read_text(encoding="utf-8"))
    metadata = dict(post.metadata)
    metadata["review_id"] = str(metadata.get("review_id") or path.stem)
    metadata["kind"] = str(metadata.get("kind") or _kind_from_review_id(path.stem))
    metadata["status"] = status.value
    metadata["decision"] = action.value
    metadata["decided_at"] = _now_utc().isoformat()
    rendered = frontmatter.dumps(frontmatter.Post(post.content, **metadata), sort_keys=False)
    _write_lf(path, rendered)


def _extract_review_data(kind: ReviewKind, content: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    json_values = _json_fence_values(content, errors)
    key_values = _markdown_key_values(content)
    data: dict[str, Any] = {"json": json_values, **key_values}

    if kind in {
        ReviewKind.FACT_CONFLICT,
        ReviewKind.LOW_CONFIDENCE_FACT,
        ReviewKind.PENDING_FACT,
    }:
        candidate = _candidate_json(json_values)
        if candidate is not None:
            data["candidate"] = candidate
        active_facts = _active_facts_json(json_values)
        if active_facts is not None:
            data["active_facts"] = active_facts
        pending_payload = _pending_payload_json(json_values)
        if pending_payload is not None:
            data.update(pending_payload)
    elif kind is ReviewKind.TIER_PROPOSAL:
        data.update(_tier_json(json_values))
    elif kind is ReviewKind.PROCEDURE_CANDIDATE:
        data.update(_procedure_candidate_json(json_values))

    return data, errors


def _candidate_from_data(data: dict[str, Any]) -> tuple[FactCandidate | None, list[str]]:
    if "candidate" not in data:
        return None, []
    try:
        return FactCandidate.model_validate(data["candidate"]), []
    except ValidationError as exc:
        return None, [f"Invalid fact candidate: {exc}"]


def _active_facts_from_data(data: dict[str, Any]) -> tuple[list[Fact], list[str]]:
    values = data.get("active_facts")
    if not isinstance(values, list):
        return [], []
    facts: list[Fact] = []
    errors: list[str] = []
    for value in values:
        try:
            facts.append(Fact.model_validate(value))
        except ValidationError as exc:
            errors.append(f"Invalid active fact: {exc}")
    return facts, errors


def _json_fence_values(content: str, errors: list[str]) -> list[Any]:
    values: list[Any] = []
    for match in JSON_FENCE_RE.finditer(content):
        try:
            values.append(json.loads(match.group("body").strip()))
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid JSON review payload: {exc}")
    return values


def _candidate_json(values: list[Any]) -> dict[str, Any] | None:
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("candidate"), dict):
            return dict(value["candidate"])
        if isinstance(value, dict) and _looks_like_candidate(value):
            return dict(value)
    return None


def _active_facts_json(values: list[Any]) -> list[Any] | None:
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("active_facts"), list):
            return list(value["active_facts"])
        if isinstance(value, list):
            return value
    return None


def _tier_json(values: list[Any]) -> dict[str, Any]:
    tier_keys = {"entity_id", "current_tier", "proposed_tier", "reason", "mention_count"}
    for value in values:
        if not isinstance(value, dict):
            continue
        if tier_keys.intersection(value):
            return dict(value)
        proposal = value.get("proposal")
        if isinstance(proposal, dict) and tier_keys.intersection(proposal):
            return dict(proposal)
    return {}


def _procedure_candidate_json(values: list[Any]) -> dict[str, Any]:
    for value in values:
        if not isinstance(value, dict):
            continue
        candidate = value.get("candidate")
        if isinstance(candidate, dict):
            payload: dict[str, Any] = {"candidate": dict(candidate)}
            if isinstance(value.get("event"), dict):
                payload["event"] = value["event"]
            return payload
        if {"suggested_slug", "title", "summary"}.issubset(value):
            return {"candidate": dict(value)}
    return {}


def _pending_payload_json(values: list[Any]) -> dict[str, Any] | None:
    for value in values:
        if not isinstance(value, dict):
            continue
        payload: dict[str, Any] = {}
        if isinstance(value.get("event"), dict):
            payload["event"] = value["event"]
        if isinstance(value.get("timeline_summary"), str):
            payload["timeline_summary"] = value["timeline_summary"]
        if isinstance(value.get("suggested_page_type"), str | type(None)):
            payload["suggested_page_type"] = value.get("suggested_page_type")
        if isinstance(value.get("unresolved_entities"), list):
            payload["unresolved_entities"] = value["unresolved_entities"]
        if payload:
            return payload
    return None


def _procedure_candidate_event(decision: ReviewDecision) -> Event:
    value = decision.data.get("event")
    if isinstance(value, dict):
        try:
            return Event.model_validate(value)
        except ValidationError:
            pass
    candidate = decision.data.get("candidate")
    if isinstance(candidate, dict):
        source_event = str(candidate.get("source_event") or "").strip()
        source_ref = str(candidate.get("source_ref") or "").strip()
        if source_ref:
            try:
                return Event(
                    id=source_event or str(ulid.ULID()),
                    timestamp=_now_utc(),
                    kind=EventKind.LAUNDRY_INGESTED,
                    source_ref=source_ref,
                    metadata={"source": "procedure_candidate"},
                )
            except ValidationError:
                pass
    return Event(
        id=str(ulid.ULID()),
        timestamp=_now_utc(),
        kind=EventKind.REVIEW_DECIDED,
        source_ref=_review_source_ref(BrainPaths(_infer_brain_root(decision.path)), decision.path),
    )


def _procedure_candidate_truth(summary: str, steps: list[str]) -> str:
    if not steps:
        return summary
    return "\n".join(
        [
            summary,
            "",
            "Steps:",
            *[f"{index}. {step}" for index, step in enumerate(steps, start=1)],
        ]
    )


def _looks_like_candidate(value: dict[str, Any]) -> bool:
    keys = {"subject", "predicate", "object", "object_type", "source_event", "confidence"}
    return keys.issubset(value)


def _markdown_key_values(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        match = KEY_VALUE_RE.match(line)
        if match is not None:
            values[match.group("key").replace("-", "_")] = match.group("value").strip()
    return values


def _selected_action(content: str) -> tuple[ReviewAction, list[str]]:
    actions: list[ReviewAction] = []
    for line in content.splitlines():
        match = CHECKBOX_RE.match(line)
        if match is not None:
            actions.append(_action_from_label(match.group("label")))

    if not actions:
        return ReviewAction.NONE, []

    unique = list(dict.fromkeys(actions))
    if len(unique) > 1:
        labels = ", ".join(action.value for action in unique)
        return ReviewAction.NONE, [f"Multiple review decisions selected: {labels}"]
    return unique[0], []


def _action_from_label(label: str) -> ReviewAction:
    normalized = label.strip().lower()
    if "reject" in normalized or "rejected" in normalized:
        return ReviewAction.REJECT
    if "defer" in normalized or "deferred" in normalized:
        return ReviewAction.DEFER
    if (
        "approve" in normalized
        or "approved" in normalized
        or "accept" in normalized
    ):
        return ReviewAction.APPROVE
    raise BrainError(f"Unknown review decision label: {label}")


def _coerce_kind(value: object) -> ReviewKind:
    text = str(value or "").strip()
    aliases = {
        "low_confidence": ReviewKind.LOW_CONFIDENCE_FACT.value,
        "low_confidence_fact": ReviewKind.LOW_CONFIDENCE_FACT.value,
    }
    text = aliases.get(text, text)
    try:
        return ReviewKind(text)
    except ValueError as exc:
        raise BrainError(f"Unsupported review kind: {value}") from exc


def _coerce_status(value: object) -> ReviewStatus:
    text = str(value or ReviewStatus.PENDING.value).strip()
    try:
        return ReviewStatus(text)
    except ValueError:
        return ReviewStatus.PENDING


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _kind_from_review_id(review_id: str) -> str:
    match = REVIEW_ID_RE.match(review_id)
    if match is not None:
        return match.group("kind")
    parts = review_id.split("_", maxsplit=2)
    if len(parts) == 3:
        return parts[2]
    raise BrainError(f"Could not infer review kind from id: {review_id}")


def _review_file_refs(paths: BrainPaths, path: Path) -> tuple[str, ...]:
    refs = [path.as_posix(), path.name]
    with suppress(ValueError):
        refs.append(path.relative_to(paths.root).as_posix())

    deduped: list[str] = []
    for ref in refs:
        if ref not in deduped:
            deduped.append(ref)
    return tuple(deduped)


def _resolve_pending_candidate(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
) -> FactCandidate | None:
    subject = _resolve_existing_entity_id(conn, candidate.subject)
    if subject is None:
        return None

    object_value = candidate.object
    if candidate.object_type.value == "entity":
        resolved_object = _resolve_existing_entity_id(conn, candidate.object)
        if resolved_object is None:
            return None
        object_value = resolved_object

    return candidate.model_copy(update={"subject": subject, "object": object_value})


def _resolve_existing_entity_id(conn: sqlite3.Connection, value: str) -> str | None:
    if get_entity(conn, value) is not None:
        return value

    alias_id = lookup_by_alias(conn, value)
    if alias_id is not None:
        return alias_id

    row = conn.execute(
        "SELECT id FROM entities WHERE title = ? ORDER BY id LIMIT 1",
        (value,),
    ).fetchone()
    if row is None:
        return None
    return str(row["id"])


def _pending_event(decision: ReviewDecision, candidate: FactCandidate) -> Event:
    value = decision.data.get("event")
    if isinstance(value, dict):
        try:
            return Event.model_validate(value)
        except ValidationError:
            pass
    return Event(
        id=candidate.source_event,
        timestamp=_now_utc(),
        kind=EventKind.REVIEW_DECIDED,
        source_ref=candidate.source_ref or _review_source_ref(
            BrainPaths(_infer_brain_root(decision.path)),
            decision.path,
        ),
    )


def _page_type_data(decision: ReviewDecision) -> PageType | None:
    value = decision.data.get("suggested_page_type")
    if value is None:
        return None
    try:
        return PageType(str(value))
    except ValueError:
        return None


def _entity_type_data(decision: ReviewDecision) -> EntityType | None:
    value = _string_data(decision, "type")
    if value in {None, "None", "null"}:
        return None
    try:
        return EntityType(str(value))
    except ValueError:
        return None


def _valid_slug(value: str) -> bool:
    return re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is not None


def _default_page_path(entity_id: str, entity_type: EntityType) -> str:
    if entity_type is EntityType.PROJECT:
        return f"pages/projects/{entity_id}.md"
    if entity_type is EntityType.CONCEPT:
        return f"pages/concepts/{entity_id}.md"
    if entity_type is EntityType.EVENT:
        return f"pages/events/{entity_id}.md"
    return f"pages/entities/{entity_id}.md"


def _add_alias_if_missing(conn: sqlite3.Connection, alias: str, entity_id: str) -> None:
    if alias == entity_id:
        return
    existing = lookup_by_alias(conn, alias)
    if existing == entity_id:
        return
    if existing is not None:
        raise BrainError(f"Alias already belongs to another entity: {alias}")
    add_alias(conn, alias, entity_id, EntityAliasSource.MANUAL)


def _review_source_ref(paths: BrainPaths, path: Path) -> str:
    root = paths.root.resolve()
    review_path = Path(path).resolve()
    with suppress(ValueError):
        return review_path.relative_to(root).as_posix()
    return f"external_review:{Path(path).name}"


def _string_data(decision: ReviewDecision, key: str) -> str | None:
    value = decision.data.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_data(decision: ReviewDecision, key: str) -> int | None:
    value = decision.data.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_brain_root(path: Path) -> Path:
    return _find_review_dir(Path(path)).parent


def _find_review_dir(path: Path) -> Path:
    resolved = Path(path)
    for candidate in [resolved.parent, *resolved.parents]:
        if candidate.name == "review":
            return candidate
    raise BrainError(f"Could not infer brain root from review path: {path}")


def _unique_archive_path(path: Path) -> Path:
    if not path.exists():
        return path

    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise BrainError(f"Could not find archive name for review file: {path}")


def _checkpoint_and_close(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _auto_commit(paths: BrainPaths, applied_count: int) -> None:
    config = load_config(paths.config_path)
    if not config.git.auto_commit:
        return
    commit(
        paths.root,
        f"review: apply {applied_count} decisions",
        paths=_commit_paths(paths),
    )


def _commit_paths(paths: BrainPaths) -> list[Path]:
    candidates = [
        paths.db_path,
        paths.events_jsonl,
        paths.pages_dir,
        paths.review_dir,
    ]
    return [path for path in candidates if path.exists()]


def _remove_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        try:
            sidecar.unlink()
        except (FileNotFoundError, PermissionError):
            continue


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _write_lf(path: Path, text: str) -> None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    path.write_text(normalized, encoding="utf-8", newline="\n")
