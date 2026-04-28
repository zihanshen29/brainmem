from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import frontmatter
from pydantic import BaseModel, ConfigDict, Field

from brain.config import load_config
from brain.db.connection import connect
from brain.exceptions import BrainError
from brain.git_ops import commit
from brain.models import Tier
from brain.pages import parse_page
from brain.paths import BrainPaths


class LintKind(StrEnum):
    """Supported deterministic lint checks."""

    CONTRADICTIONS = "contradictions"
    STALE = "stale"
    ORPHANS = "orphans"
    CITATIONS = "citations"


class LintIssue(BaseModel):
    """One lint finding suitable for review."""

    model_config = ConfigDict(extra="forbid")

    kind: LintKind
    message: str = Field(..., min_length=1)
    page: str | None = None
    slug: str | None = None
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class LintRunReport(BaseModel):
    """Aggregate output from a lint run."""

    model_config = ConfigDict(extra="forbid")

    kinds: list[LintKind] = Field(default_factory=list)
    issue_count: int = 0
    issues_by_kind: dict[LintKind, int] = Field(default_factory=dict)
    review_files: list[str] = Field(default_factory=list)
    lint_results: list[int] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]+)?\]\]")
REVIEW_DECISION_SECTION = """## Decision

[ ] approve
[ ] reject
[ ] defer
"""


def lint_contradictions(conn: sqlite3.Connection) -> list[LintIssue]:
    """Find active facts with the same subject/predicate but different objects."""
    rows = conn.execute(
        """
        SELECT subject, predicate, COUNT(DISTINCT object) AS object_count
        FROM facts
        WHERE superseded_by IS NULL
          AND valid_to IS NULL
        GROUP BY subject, predicate
        HAVING object_count > 1
        ORDER BY subject, predicate
        """
    ).fetchall()

    issues: list[LintIssue] = []
    for row in rows:
        fact_rows = conn.execute(
            """
            SELECT id, object, object_type, valid_from, source_event, source_ref, confidence
            FROM facts
            WHERE subject = ?
              AND predicate = ?
              AND superseded_by IS NULL
              AND valid_to IS NULL
            ORDER BY object, id
            """,
            (row["subject"], row["predicate"]),
        ).fetchall()
        objects = sorted({str(fact["object"]) for fact in fact_rows})
        issues.append(
            LintIssue(
                kind=LintKind.CONTRADICTIONS,
                message=(
                    f"Active facts disagree for "
                    f"{row['subject']} / {row['predicate']}: {', '.join(objects)}"
                ),
                subject=str(row["subject"]),
                predicate=str(row["predicate"]),
                details={
                    "objects": objects,
                    "facts": [_row_dict(fact) for fact in fact_rows],
                },
            )
        )
    return issues


def lint_stale(
    paths: BrainPaths,
    stale_days: int,
) -> list[LintIssue]:
    """Find tier 1 pages without recent timeline entries."""
    cutoff = _today_utc() - timedelta(days=stale_days)
    issues: list[LintIssue] = []
    for page_path, page in _iter_pages(paths):
        if page.frontmatter.tier is not Tier.TIER_1:
            continue
        newest = _newest_timeline_date(page.timeline)
        if newest is not None and newest >= cutoff:
            continue
        relative = _relative(paths, page_path)
        issues.append(
            LintIssue(
                kind=LintKind.STALE,
                message=f"Tier 1 page has no timeline entry since {cutoff.isoformat()}",
                page=relative,
                slug=page.frontmatter.slug,
                details={
                    "title": page.frontmatter.title,
                    "stale_days": stale_days,
                    "cutoff": cutoff.isoformat(),
                    "newest_timeline_date": newest.isoformat() if newest else None,
                },
            )
        )
    return issues


def lint_orphans(conn: sqlite3.Connection, paths: BrainPaths) -> list[LintIssue]:
    """Find timeline wikilinks whose slug has no entity or no entity page."""
    entities = _entity_page_index(conn, paths)
    issues: list[LintIssue] = []
    seen: set[tuple[str, str, str]] = set()

    for page_path, page in _iter_pages(paths):
        relative = _relative(paths, page_path)
        for line_number, line in enumerate(page.timeline, start=1):
            for slug in _wikilinks(line):
                entity_page = entities.get(slug)
                reason = None
                if slug not in entities:
                    reason = "missing_entity"
                elif entity_page is None or not entity_page.exists():
                    reason = "missing_page"
                if reason is None:
                    continue

                key = (relative, slug, reason)
                if key in seen:
                    continue
                seen.add(key)
                issues.append(
                    LintIssue(
                        kind=LintKind.ORPHANS,
                        message=f"Timeline links to [[{slug}]] but {reason.replace('_', ' ')}",
                        page=relative,
                        slug=slug,
                        details={"reason": reason, "line_number": line_number, "line": line},
                    )
                )
    return issues


def lint_citations(paths: BrainPaths) -> list[LintIssue]:
    """Find compiled-truth wikilinks that are not supported by timeline wikilinks."""
    issues: list[LintIssue] = []
    for page_path, page in _iter_pages(paths):
        compiled_links = set(_wikilinks(page.compiled_truth))
        timeline_links = set(_wikilinks("\n".join(page.timeline)))
        missing = sorted(compiled_links - timeline_links)
        if not missing:
            continue
        relative = _relative(paths, page_path)
        issues.append(
            LintIssue(
                kind=LintKind.CITATIONS,
                message="Compiled truth contains wikilinks not cited in the timeline",
                page=relative,
                slug=page.frontmatter.slug,
                details={
                    "missing_timeline_links": missing,
                    "compiled_truth_links": sorted(compiled_links),
                    "timeline_links": sorted(timeline_links),
                },
            )
        )
    return issues


def run_lint(
    brain_root: Path,
    kinds: Iterable[str | LintKind],
    stale_days: int | None = None,
) -> LintRunReport:
    """Run selected lint checks, record lint_results, and create review files."""
    paths = BrainPaths(Path(brain_root))
    config = load_config(paths.config_path)
    requested = list(kinds)
    selected = _normalize_kinds(requested)
    requested_all = _is_all_request(requested)
    effective_stale_days = stale_days if stale_days is not None else config.lint.stale_days
    if effective_stale_days < 1:
        raise BrainError("stale_days must be positive")

    report = LintRunReport(kinds=selected)
    conn = connect(paths.db_path)
    try:
        for kind in selected:
            issues = _run_one_kind(conn, paths, kind, effective_stale_days)
            review_file = _write_review(paths, kind, issues) if issues else ""
            result_id = _insert_lint_result(conn, kind, len(issues), review_file)
            conn.commit()

            report.issue_count += len(issues)
            report.issues_by_kind[kind] = len(issues)
            report.lint_results.append(result_id)
            if review_file:
                report.review_files.append(review_file)
    finally:
        _checkpoint_and_close(conn)
        _remove_sqlite_sidecars(paths.db_path)

    if config.git.auto_commit and (report.lint_results or report.review_files):
        commit_label = "all" if requested_all else selected[0].value
        commit(
            paths.root,
            f"lint: {commit_label}, {report.issue_count} issues found",
            paths=_commit_paths(paths),
        )
    return report


def _run_one_kind(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    kind: LintKind,
    stale_days: int,
) -> list[LintIssue]:
    if kind is LintKind.CONTRADICTIONS:
        return lint_contradictions(conn)
    if kind is LintKind.STALE:
        return lint_stale(paths, stale_days)
    if kind is LintKind.ORPHANS:
        return lint_orphans(conn, paths)
    if kind is LintKind.CITATIONS:
        return lint_citations(paths)
    raise BrainError(f"Unsupported lint kind: {kind}")


def _normalize_kinds(kinds: Iterable[str | LintKind]) -> list[LintKind]:
    values = list(kinds)
    if not values:
        raise BrainError("At least one lint kind is required")

    normalized: list[LintKind] = []
    for value in values:
        text = value.value if isinstance(value, LintKind) else str(value)
        if text == "all":
            return list(LintKind)
        try:
            kind = LintKind(text)
        except ValueError as exc:
            raise BrainError(f"Unsupported lint kind: {value}") from exc
        if kind not in normalized:
            normalized.append(kind)
    return normalized


def _is_all_request(kinds: Iterable[str | LintKind]) -> bool:
    return any((kind.value if isinstance(kind, LintKind) else str(kind)) == "all" for kind in kinds)


def _write_review(paths: BrainPaths, lint_kind: LintKind, issues: list[LintIssue]) -> str:
    created_at = _now_utc()
    review_id = f"{created_at.date().isoformat()}_{_next_review_seq(paths.review_dir, created_at.date().isoformat()):03d}_lint_finding"
    path = paths.review_dir / f"{review_id}.md"
    metadata = {
        "review_id": review_id,
        "kind": "lint_finding",
        "lint_kind": lint_kind.value,
        "issue_count": len(issues),
        "created": created_at.isoformat(),
        "status": "pending",
    }
    body = "\n".join(
        [
            f"# Lint finding: {lint_kind.value}",
            "",
            f"{len(issues)} issue(s) found.",
            "",
            "## Issues",
            "",
            "```json",
            json.dumps(
                [issue.model_dump(mode="json") for issue in issues],
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            REVIEW_DECISION_SECTION,
        ]
    )
    rendered = frontmatter.dumps(frontmatter.Post(body, **metadata), sort_keys=False)
    _write_lf(path, rendered)
    return path.relative_to(paths.root).as_posix()


def _insert_lint_result(
    conn: sqlite3.Connection,
    kind: LintKind,
    issue_count: int,
    report_file: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO lint_results (run_at, kind, issue_count, report_file)
        VALUES (?, ?, ?, ?)
        """,
        (_now_utc().isoformat(), kind.value, issue_count, report_file),
    )
    return int(cursor.lastrowid)


def _iter_pages(paths: BrainPaths):
    if not paths.pages_dir.exists():
        return
    for page_path in sorted(paths.pages_dir.rglob("*.md")):
        if page_path.name in {"index.md", "log.md"}:
            continue
        with suppress(Exception):
            yield page_path, parse_page(page_path)


def _entity_page_index(conn: sqlite3.Connection, paths: BrainPaths) -> dict[str, Path | None]:
    rows = conn.execute("SELECT id, page_path FROM entities ORDER BY id").fetchall()
    return {str(row["id"]): _entity_page_path(paths, row["id"], row["page_path"]) for row in rows}


def _entity_page_path(paths: BrainPaths, entity_id: str, page_path: str | None) -> Path | None:
    candidates: list[Path] = []
    if page_path:
        configured = Path(page_path)
        candidates.append(configured if configured.is_absolute() else paths.root / configured)
    candidates.append(paths.entities_dir / f"{entity_id}.md")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else None


def _wikilinks(text: str) -> list[str]:
    links: list[str] = []
    for match in WIKILINK_RE.finditer(text):
        slug = match.group(1).strip()
        if slug:
            links.append(slug)
    return links


def _newest_timeline_date(lines: list[str]) -> date | None:
    newest: date | None = None
    for line in lines:
        parts = line.split(maxsplit=2)
        if len(parts) < 2:
            continue
        with suppress(ValueError):
            parsed = date.fromisoformat(parts[1])
            newest = parsed if newest is None else max(newest, parsed)
    return newest


def _next_review_seq(review_dir: Path, day: str) -> int:
    if not review_dir.exists():
        return 1
    max_seq = 0
    for path in review_dir.glob(f"{day}_*_*.md"):
        parts = path.stem.split("_", maxsplit=2)
        if len(parts) < 3:
            continue
        with suppress(ValueError):
            max_seq = max(max_seq, int(parts[1]))
    return max_seq + 1


def _commit_paths(paths: BrainPaths) -> list[Path]:
    candidates = [paths.db_path, paths.review_dir]
    return [path for path in candidates if path.exists()]


def _checkpoint_and_close(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _remove_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        try:
            sidecar.unlink()
        except (FileNotFoundError, PermissionError):
            continue


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(zip(row.keys(), row, strict=True))


def _relative(paths: BrainPaths, path: Path) -> str:
    with suppress(ValueError):
        return path.relative_to(paths.root).as_posix()
    return path.as_posix()


def _today_utc() -> date:
    return _now_utc().date()


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    path.write_text(normalized, encoding="utf-8", newline="\n")
