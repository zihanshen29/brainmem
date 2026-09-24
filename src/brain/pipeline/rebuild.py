from __future__ import annotations

import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from brain.concurrency import coordinated
from brain.config import load_config
from brain.db.backlinks import replace_backlinks_for_page
from brain.db.connection import connect
from brain.db.entities import add_alias, get_entity, lookup_by_alias, upsert_entity
from brain.db.migrations import init_db
from brain.exceptions import BrainError, ConfigError
from brain.models import Entity, EntityAliasSource, EntityType, Page, PageType, Tier
from brain.pages import parse_page, regenerate_index
from brain.paths import BrainPaths
from brain.pipeline._config import default_pipeline_config
from brain.pipeline.autolink import extract_backlinks

RebuildScope = Literal["db", "pages", "backlinks", "index", "derived"]


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


@coordinated(write=True)
def rebuild_db(brain_root: Path, *, auto_commit: bool | None = None) -> RebuildReport:
    """Refresh page-derived registry fields and indexes, preserving primary DB records."""
    paths = BrainPaths(Path(brain_root))
    report = RebuildReport(scope="db")

    if not paths.db_path.is_file():
        raise BrainError("brain.db is primary data; restore a verified backup before rebuilding")
    pages = _collect_parseable_pages(paths, report)
    if report.errors:
        raise BrainError("Cannot rebuild database: " + "; ".join(report.errors))
    entity_pages = [item for item in pages if item[1].frontmatter.type is not PageType.PROCEDURE]
    previous_index = paths.pages_index.read_bytes() if paths.pages_index.exists() else None
    with TemporaryDirectory(prefix=".rebuild-", dir=paths.root) as directory:
        staged_db = Path(directory) / "brain.db"
        with closing(connect(paths.db_path, read_only=True)) as source:
            try:
                healthy = source.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            except sqlite3.DatabaseError as exc:
                raise BrainError("Database is damaged; restore a verified backup") from exc
            if not healthy:
                raise BrainError("Database is damaged; restore a verified backup")
            with closing(sqlite3.connect(staged_db)) as destination:
                source.backup(destination)
        init_db(staged_db)
        with closing(connect(staged_db)) as conn:
            with conn:
                _rebuild_entities(conn, paths, entity_pages, report)
                report.backlinks_rebuilt = _replace_all_backlinks(conn, pages)
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BrainError("Rebuilt database failed integrity check")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BrainError("Rebuilt database failed foreign key check")
        try:
            regenerate_index(paths.root)
            _publish_rebuilt_db(staged_db, paths.db_path)
        except Exception:
            if previous_index is None:
                paths.pages_index.unlink(missing_ok=True)
            else:
                paths.pages_index.write_bytes(previous_index)
            raise
        report.index_rebuilt = True

    report.committed = _maybe_commit(
        paths,
        auto_commit,
        "rebuild: db",
        [paths.db_path, paths.pages_index],
    )
    return _sorted_report(report)


@coordinated(write=True)
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

    from brain.pipeline.summaries import propose_summary
    draft = propose_summary(Path(brain_root), slug, provider=False)
    return RebuildReport(scope="pages", pages_touched=[draft["review_file"]])


@coordinated(write=True)
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

    report.committed = _maybe_commit(
        paths,
        auto_commit,
        "rebuild: backlinks",
        [paths.db_path],
    )
    return _sorted_report(report)


@coordinated(write=True)
def rebuild_derived(
    brain_root: Path,
    *,
    auto_commit: bool | None = None,
) -> RebuildReport:
    """Rebuild deterministic derived indexes: backlinks and pages/index.md."""
    paths = BrainPaths(Path(brain_root))
    report = RebuildReport(scope="derived")
    conn = connect(paths.db_path)
    try:
        pages = _collect_parseable_pages(paths, report)
        with conn:
            conn.execute("DELETE FROM backlinks")
            report.backlinks_rebuilt = _replace_backlinks_for_pages(conn, pages)
        _finalize_db(conn, paths.db_path)
    finally:
        conn.close()

    regenerate_index(paths.root)
    report.index_rebuilt = True
    report.pages_touched.append(paths.pages_index.relative_to(paths.root).as_posix())
    report.committed = _maybe_commit(
        paths,
        auto_commit,
        "rebuild: derived indexes",
        [paths.db_path, paths.pages_index],
    )
    return _sorted_report(report)


@coordinated(write=True)
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
        existing = get_entity(conn, frontmatter.slug)
        entity = Entity(
            id=frontmatter.slug,
            type=page_entity_type(page, existing),
            title=frontmatter.title,
            page_path=page_path.relative_to(paths.root).as_posix(),
            tier=frontmatter.tier or (existing.tier if existing else Tier.TIER_3),
            mention_count=existing.mention_count if existing else 0,
            first_seen=existing.first_seen if existing else frontmatter.created,
            last_seen=existing.last_seen if existing else frontmatter.updated,
            metadata=existing.metadata if existing else {},
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
        owner = lookup_by_alias(conn, alias)
        if owner == entity_id:
            continue
        if owner is not None:
            raise BrainError(f"Alias {alias!r} already belongs to {owner}")
        add_alias(conn, alias, entity_id, EntityAliasSource.FRONTMATTER)
        report.aliases_rebuilt += 1


def page_entity_type(page: Page, existing: Entity | None = None) -> EntityType:
    if page.frontmatter.entity_type:
        return EntityType(page.frontmatter.entity_type)
    mapping = {PageType.PROJECT: EntityType.PROJECT, PageType.CONCEPT: EntityType.CONCEPT,
               PageType.EVENT: EntityType.EVENT}
    if page.frontmatter.type in mapping:
        return mapping[page.frontmatter.type]
    explicit = _entity_type_from_tags(page.frontmatter.tags)
    return explicit if explicit is not EntityType.UNKNOWN else (existing.type if existing else explicit)


def _entity_type_from_tags(tags: list[str]) -> EntityType:
    for value in ("person", "org", "concept", "project", "event", "place"):
        if value in tags:
            return EntityType(value)
    return EntityType.UNKNOWN


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


def _publish_rebuilt_db(staged_db: Path, db_path: Path) -> None:
    """Let SQLite replace a healthy destination transactionally, honoring its locks."""
    def check_busy(status: int, remaining: int, total: int) -> None:
        if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise BrainError("Database is busy; stop other writers before rebuilding")

    with closing(sqlite3.connect(db_path)) as destination:
        destination.execute("PRAGMA user_version").fetchone()
        with closing(sqlite3.connect(staged_db)) as source:
            source.backup(destination, pages=128, progress=check_busy)


def _finalize_db(conn: sqlite3.Connection, db_path: Path) -> None:
    with suppress(sqlite3.Error):
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


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
