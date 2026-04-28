from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from brain.config import load_config
from brain.db.backlinks import replace_backlinks_for_page
from brain.db.connection import connect
from brain.db.entities import add_alias, upsert_entity
from brain.db.migrations import init_db
from brain.exceptions import BrainError, ConfigError
from brain.llm import client as llm_client
from brain.models import Entity, EntityAliasSource, EntityType, Page, PageType, Tier
from brain.pages import parse_page, regenerate_index, write_page
from brain.pages.timeline import parse_entry
from brain.paths import BrainPaths
from brain.pipeline._config import default_pipeline_config
from brain.pipeline.autolink import extract_backlinks

RebuildScope = Literal["db", "pages", "backlinks", "index"]


class RebuildReport(BaseModel):
    """Summary of one deterministic rebuild operation."""

    model_config = ConfigDict(extra="forbid")

    scope: RebuildScope
    pages_scanned: int = 0
    pages_touched: list[str] = Field(default_factory=list)
    entities_rebuilt: int = 0
    aliases_rebuilt: int = 0
    backlinks_rebuilt: int = 0
    index_rebuilt: bool = False
    facts_rebuilt: int = 0
    committed: bool = False
    errors: list[str] = Field(default_factory=list)


def rebuild_db(brain_root: Path, *, auto_commit: bool | None = None) -> RebuildReport:
    """Rebuild brain.db from current markdown pages, then rebuild backlinks and index."""
    paths = BrainPaths(Path(brain_root))
    report = RebuildReport(scope="db")

    _remove_db_files(paths.db_path)
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(paths.db_path)

    conn = connect(paths.db_path)
    try:
        pages = _collect_parseable_pages(paths, report)
        entity_pages = [
            item for item in pages if item[1].frontmatter.type is PageType.ENTITY
        ]
        with conn:
            _rebuild_entities(conn, paths, entity_pages, report)
            report.backlinks_rebuilt = _replace_all_backlinks(conn, pages)

        regenerate_index(paths.root)
        report.index_rebuilt = True
        _finalize_db(conn, paths.db_path)
    finally:
        conn.close()
        _remove_sqlite_sidecars(paths.db_path)

    report.committed = _maybe_commit(
        paths,
        auto_commit,
        "rebuild: db",
        [paths.db_path, paths.pages_index],
    )
    return _sorted_report(report)


def rebuild_pages(
    brain_root: Path,
    slug: str,
    *,
    force: bool = False,
    auto_commit: bool | None = None,
) -> RebuildReport:
    """Rewrite one existing page's compiled truth from its timeline."""
    if not force:
        raise BrainError("rebuild_pages requires force=True")

    paths = BrainPaths(Path(brain_root))
    report = RebuildReport(scope="pages")
    page_path, page = _resolve_unique_page(paths, slug)

    timeline = [parse_entry(line) for line in page.timeline]
    compiled_truth = llm_client.rewrite_compiled_truth(timeline, page.compiled_truth)
    updated_page = page.model_copy(
        update={
            "frontmatter": page.frontmatter.model_copy(update={"updated": _now_utc()}),
            "compiled_truth": compiled_truth,
        }
    )
    write_page(page_path, updated_page)
    report.pages_touched.append(page_path.relative_to(paths.root).as_posix())

    conn = connect(paths.db_path)
    try:
        parsed_page = parse_page(page_path)
        with conn:
            report.backlinks_rebuilt = _replace_backlinks_for_pages(
                conn,
                [(page_path, parsed_page)],
            )
        regenerate_index(paths.root)
        report.index_rebuilt = True
    finally:
        _finalize_db(conn, paths.db_path)
        conn.close()
        _remove_sqlite_sidecars(paths.db_path)

    report.committed = _maybe_commit(
        paths,
        auto_commit,
        f"rebuild: page {slug}",
        [paths.db_path, paths.pages_index, page_path],
    )
    return _sorted_report(report)


def rebuild_backlinks(
    brain_root: Path,
    *,
    auto_commit: bool | None = None,
) -> RebuildReport:
    """Rebuild backlinks from current markdown using existing DB entities and aliases."""
    paths = BrainPaths(Path(brain_root))
    report = RebuildReport(scope="backlinks")
    conn = connect(paths.db_path)
    try:
        pages = _collect_parseable_pages(paths, report)
        with conn:
            conn.execute("DELETE FROM backlinks")
            report.backlinks_rebuilt = _replace_backlinks_for_pages(conn, pages)
        _finalize_db(conn, paths.db_path)
    finally:
        conn.close()
        _remove_sqlite_sidecars(paths.db_path)

    report.committed = _maybe_commit(
        paths,
        auto_commit,
        "rebuild: backlinks",
        [paths.db_path],
    )
    return _sorted_report(report)


def rebuild_index(brain_root: Path, *, auto_commit: bool | None = None) -> RebuildReport:
    """Regenerate pages/index.md."""
    paths = BrainPaths(Path(brain_root))
    report = RebuildReport(scope="index")
    regenerate_index(paths.root)
    report.index_rebuilt = True
    report.pages_touched.append(paths.pages_index.relative_to(paths.root).as_posix())
    report.committed = _maybe_commit(
        paths,
        auto_commit,
        "rebuild: index",
        [paths.pages_index],
    )
    return _sorted_report(report)


def _collect_parseable_pages(paths: BrainPaths, report: RebuildReport) -> list[tuple[Path, Page]]:
    items: list[tuple[Path, Page]] = []
    for path in _canonical_page_paths(paths):
        report.pages_scanned += 1
        try:
            items.append((path, parse_page(path)))
        except BrainError as exc:
            report.errors.append(f"{path.relative_to(paths.root).as_posix()}: {exc}")
    return items


def _canonical_page_paths(paths: BrainPaths) -> list[Path]:
    if not paths.pages_dir.exists():
        return []
    return sorted(
        path
        for path in paths.pages_dir.glob("**/*.md")
        if path.is_file() and path.name not in {"index.md", "log.md"}
    )


def _rebuild_entities(
    conn: sqlite3.Connection,
    paths: BrainPaths,
    pages: list[tuple[Path, Page]],
    report: RebuildReport,
) -> None:
    alias_owners: dict[str, str] = {}
    alias_rows: set[tuple[str, str]] = set()

    for page_path, page in pages:
        frontmatter = page.frontmatter
        entity = Entity(
            id=frontmatter.slug,
            type=_entity_type_from_tags(frontmatter.tags),
            title=frontmatter.title,
            page_path=page_path.relative_to(paths.root).as_posix(),
            tier=frontmatter.tier or Tier.TIER_3,
            mention_count=0,
            first_seen=frontmatter.created,
            last_seen=frontmatter.updated,
        )
        upsert_entity(conn, entity)
        report.entities_rebuilt += 1

        for alias in frontmatter.aliases:
            owner = alias_owners.get(alias)
            if owner is not None and owner != entity.id:
                raise BrainError(f"Alias {alias!r} points to multiple entities: {owner}, {entity.id}")
            alias_owners[alias] = entity.id
            alias_rows.add((alias, entity.id))

    for alias, entity_id in sorted(alias_rows):
        add_alias(conn, alias, entity_id, EntityAliasSource.FRONTMATTER)
        report.aliases_rebuilt += 1


def _entity_type_from_tags(tags: list[str]) -> EntityType:
    for value in ("person", "org", "concept", "project", "event", "place"):
        if value in tags:
            return EntityType(value)
    return EntityType.CONCEPT


def _replace_all_backlinks(conn: sqlite3.Connection, pages: list[tuple[Path, Page]]) -> int:
    conn.execute("DELETE FROM backlinks")
    return _replace_backlinks_for_pages(conn, pages)


def _replace_backlinks_for_pages(
    conn: sqlite3.Connection,
    pages: list[tuple[Path, Page]],
) -> int:
    alias_map, entity_types = _load_alias_map(conn)
    rebuilt = 0
    for page_path, page in pages:
        content = page_path.read_text(encoding="utf-8")
        links = extract_backlinks(
            content,
            alias_map=alias_map,
            from_page=page.frontmatter.slug,
            from_page_type=page.frontmatter.type,
            entity_types=entity_types,
        )
        extracted_at = page.frontmatter.updated or page.frontmatter.created
        links = [
            link.model_copy(update={"extracted_at": extracted_at})
            for link in links
            if link.to_entity in entity_types
        ]
        replace_backlinks_for_page(conn, page.frontmatter.slug, links)
        rebuilt += len(links)
    _update_mention_counts(conn)
    return rebuilt


def _load_alias_map(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[str, EntityType]]:
    alias_rows = conn.execute("SELECT alias, entity_id FROM entity_aliases").fetchall()
    entity_rows = conn.execute("SELECT id, title, type FROM entities").fetchall()

    alias_map = {row["alias"]: row["entity_id"] for row in alias_rows}
    entity_types: dict[str, EntityType] = {}
    for row in entity_rows:
        entity_id = str(row["id"])
        alias_map.setdefault(str(row["title"]), entity_id)
        alias_map.setdefault(entity_id, entity_id)
        entity_types[entity_id] = EntityType(row["type"])
    return alias_map, entity_types


def _update_mention_counts(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE entities SET mention_count = 0")
    conn.execute(
        """
        UPDATE entities
        SET mention_count = (
            SELECT COUNT(*)
            FROM backlinks
            WHERE backlinks.to_entity = entities.id
        )
        """
    )


def _resolve_unique_page(paths: BrainPaths, slug: str) -> tuple[Path, Page]:
    matches: list[tuple[Path, Page]] = []
    for path in _canonical_page_paths(paths):
        page = parse_page(path)
        if page.frontmatter.slug == slug:
            matches.append((path, page))

    if not matches:
        raise BrainError(f"Page not found for slug: {slug}")
    if len(matches) > 1:
        locations = ", ".join(path.relative_to(paths.root).as_posix() for path, _ in matches)
        raise BrainError(f"Page slug is not unique: {slug} ({locations})")
    return matches[0]


def _remove_db_files(db_path: Path) -> None:
    for path in [
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    ]:
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _finalize_db(conn: sqlite3.Connection, db_path: Path) -> None:
    with suppress(sqlite3.Error):
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _remove_sqlite_sidecars(db_path)


def _remove_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        try:
            sidecar.unlink()
        except (FileNotFoundError, PermissionError):
            continue


def _maybe_commit(
    paths: BrainPaths,
    auto_commit: bool | None,
    message: str,
    commit_paths: list[Path],
) -> bool:
    try:
        config = load_config(paths.config_path)
    except ConfigError:
        config = default_pipeline_config()
    should_commit = config.git.auto_commit if auto_commit is None else auto_commit
    if not should_commit:
        return False

    from brain import git_ops

    existing_paths = [path for path in commit_paths if path.exists()]
    return git_ops.commit(paths.root, message, paths=existing_paths) is not None


def _sorted_report(report: RebuildReport) -> RebuildReport:
    report.pages_touched = sorted(report.pages_touched)
    report.errors = sorted(report.errors)
    return report


def _now_utc() -> datetime:
    return datetime.now(UTC)
