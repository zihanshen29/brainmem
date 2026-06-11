from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import ulid
from pydantic import BaseModel, ConfigDict, Field

from brain.config import load_config
from brain.db.connection import connect
from brain.exceptions import BrainError, ConfigError, DBError
from brain.ledger import append_event
from brain.models import Event, EventKind, PageType
from brain.pages import append_log, parse_page
from brain.paths import BrainPaths
from brain.pipeline._config import default_pipeline_config
from brain.pipeline.rebuild import rebuild_derived

_WIKILINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_STUB_TEXT = "(stub - waiting for more evidence)"


class EntityPruneReport(BaseModel):
    """Summary of one safe stub-prune operation."""

    model_config = ConfigDict(extra="forbid")

    slugs: list[str]
    pages_removed: list[str] = Field(default_factory=list)
    aliases_removed: int = 0
    facts_deleted: int = 0
    backlinks_deleted: int = 0
    tier_proposals_deleted: int = 0
    embeddings_deleted: int = 0
    pages_rewritten: list[str] = Field(default_factory=list)
    backlinks_rebuilt: int = 0
    index_rebuilt: bool = False
    committed: bool = False


def prune_stub_entities(
    brain_root: Path,
    slugs: list[str],
    *,
    delete_facts: bool = False,
    auto_commit: bool | None = None,
) -> EntityPruneReport:
    """Remove mistaken stub entity pages and their registry rows."""
    unique_slugs = _unique_slugs(slugs)
    if not unique_slugs:
        raise BrainError("At least one entity slug is required")

    paths = BrainPaths(Path(brain_root))
    pages = _load_prunable_pages(paths, unique_slugs)
    report = EntityPruneReport(slugs=unique_slugs)

    conn = connect(paths.db_path)
    try:
        _validate_fact_references(conn, unique_slugs, delete_facts=delete_facts)
        titles = {slug: page.frontmatter.title for slug, (_, page) in pages.items()}
        report.pages_rewritten = _unlink_wikilinks(paths, titles)

        with conn:
            for slug in unique_slugs:
                page_path, _ = pages[slug]
                report.pages_removed.append(page_path.relative_to(paths.root).as_posix())
                page_path.unlink()
                report.aliases_removed += _execute_count(
                    conn,
                    "DELETE FROM entity_aliases WHERE entity_id = ?",
                    (slug,),
                )
                if delete_facts:
                    report.facts_deleted += _execute_count(
                        conn,
                        "DELETE FROM facts WHERE subject = ? OR (object_type = 'entity' AND object = ?)",
                        (slug, slug),
                    )
                report.backlinks_deleted += _execute_count(
                    conn,
                    "DELETE FROM backlinks WHERE from_page = ? OR to_entity = ?",
                    (slug, slug),
                )
                report.tier_proposals_deleted += _execute_count(
                    conn,
                    "DELETE FROM tier_proposals WHERE entity_id = ?",
                    (slug,),
                )
                report.embeddings_deleted += _delete_embeddings(conn, slug)
                conn.execute("DELETE FROM entities WHERE id = ?", (slug,))
        _checkpoint_db(conn)
    except sqlite3.Error as exc:
        raise DBError("Could not prune stub entities") from exc
    finally:
        conn.close()
        _remove_sqlite_sidecars(paths.db_path)

    rebuild_report = rebuild_derived(paths.root, auto_commit=False)
    report.backlinks_rebuilt = rebuild_report.backlinks_rebuilt
    report.index_rebuilt = rebuild_report.index_rebuilt
    _record_prune_event(paths, report)
    append_log(
        paths.root,
        f"- {_now_utc().strftime('%Y-%m-%d %H:%M')} entity prune: removed {len(report.pages_removed)} stubs",
    )
    report.committed = _maybe_commit(paths, auto_commit, report)
    return report.model_copy(
        update={
            "pages_removed": sorted(report.pages_removed),
            "pages_rewritten": sorted(set(report.pages_rewritten)),
        }
    )


def _unique_slugs(slugs: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for slug in slugs:
        normalized = slug.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _load_prunable_pages(paths: BrainPaths, slugs: list[str]) -> dict[str, tuple[Path, object]]:
    pages: dict[str, tuple[Path, object]] = {}
    for slug in slugs:
        candidates = sorted(paths.pages_dir.glob(f"**/{slug}.md"))
        if not candidates:
            raise BrainError(f"Entity page not found: {slug}")
        if len(candidates) > 1:
            raise BrainError(f"Ambiguous entity page for slug: {slug}")
        page_path = candidates[0]
        page = parse_page(page_path)
        if page.frontmatter.slug != slug:
            raise BrainError(f"Page slug mismatch for {slug}: {page.frontmatter.slug}")
        if page.frontmatter.type not in {PageType.ENTITY, PageType.CONCEPT}:
            raise BrainError(f"Page is not an entity/concept page: {slug}")
        if page.compiled_truth.strip() != _STUB_TEXT:
            raise BrainError(f"Page is not a generated stub: {slug}")
        pages[slug] = (page_path, page)
    return pages


def _validate_fact_references(
    conn: sqlite3.Connection,
    slugs: list[str],
    *,
    delete_facts: bool,
) -> None:
    if delete_facts:
        return
    for slug in slugs:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM facts
            WHERE subject = ?
               OR (object_type = 'entity' AND object = ?)
            """,
            (slug, slug),
        ).fetchone()
        if row[0]:
            raise BrainError(f"Entity has fact references; pass --delete-facts to prune: {slug}")


def _unlink_wikilinks(paths: BrainPaths, titles: dict[str, str]) -> list[str]:
    rewritten: list[str] = []
    deleted = set(titles)
    for path in sorted(paths.pages_dir.glob("**/*.md")):
        if path.stem in deleted:
            continue
        original = path.read_text(encoding="utf-8")
        updated = _WIKILINK_PATTERN.sub(lambda match: _plain_link(match, titles), original)
        if updated == original:
            continue
        _write_lf(path, updated)
        rewritten.append(path.relative_to(paths.root).as_posix())
    return rewritten


def _plain_link(match: re.Match[str], titles: dict[str, str]) -> str:
    target = match.group(1)
    display = match.group(2)
    if target not in titles:
        return match.group(0)
    return display or titles[target]


def _delete_embeddings(conn: sqlite3.Connection, slug: str) -> int:
    rows = conn.execute(
        "SELECT rowid FROM embedding_index WHERE page_slug = ?",
        (slug,),
    ).fetchall()
    rowids = [int(row["rowid"]) for row in rows]
    for rowid in rowids:
        conn.execute("DELETE FROM embeddings WHERE rowid = ?", (rowid,))
    conn.execute("DELETE FROM embedding_index WHERE page_slug = ?", (slug,))
    return len(rowids)


def _record_prune_event(paths: BrainPaths, report: EntityPruneReport) -> None:
    append_event(
        paths.events_jsonl,
        Event(
            id=str(ulid.ULID()),
            timestamp=_now_utc(),
            kind=EventKind.PAGE_EDITED,
            source_ref="entity_prune:" + ",".join(report.slugs),
            affected_pages=report.pages_removed,
            metadata={
                "action": "entity_prune_stub",
                "slugs": report.slugs,
                "facts_deleted": report.facts_deleted,
            },
        ),
    )


def _maybe_commit(paths: BrainPaths, auto_commit: bool | None, report: EntityPruneReport) -> bool:
    try:
        config = load_config(paths.config_path)
    except ConfigError:
        config = default_pipeline_config()
    should_commit = config.git.auto_commit if auto_commit is None else auto_commit
    if not should_commit:
        return False

    from brain import git_ops

    commit_paths = [
        paths.db_path,
        paths.events_jsonl,
        paths.pages_index,
        paths.pages_log,
        *[paths.root / path for path in report.pages_removed],
        *[paths.root / path for path in report.pages_rewritten],
    ]
    return git_ops.commit(paths.root, "entity prune: remove mistaken stubs", paths=commit_paths) is not None


def _execute_count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> int:
    cursor = conn.execute(sql, params)
    return int(cursor.rowcount if cursor.rowcount is not None else 0)


def _checkpoint_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


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
    path.write_text(text, encoding="utf-8", newline="\n")
