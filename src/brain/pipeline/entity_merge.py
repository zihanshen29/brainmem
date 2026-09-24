from __future__ import annotations

import re
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import ulid
from pydantic import BaseModel, ConfigDict, Field

from brain.concurrency import coordinated
from brain.config import load_config
from brain.db.backlinks import replace_backlinks_for_page
from brain.db.connection import connect
from brain.db.embeddings import delete_page_embeddings
from brain.exceptions import BrainError, ConfigError, DBError
from brain.ledger import append_event
from brain.models import EntityAliasSource, EntityType, Event, EventKind, Page, PageType, Tier
from brain.pages import append_log, parse_page, regenerate_index, write_page
from brain.pages.timeline import format_entry, parse_entry
from brain.paths import BrainPaths
from brain.pipeline._config import default_pipeline_config
from brain.pipeline.autolink import extract_backlinks
from brain.pipeline.summaries import STUB, refresh_generated_summary

MergeDirection = Literal["a", "b"]

_WIKILINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_MISSING = object()
# Pages backed by an entity registry row; procedures and conversations are not.
_MERGEABLE_TYPES = {PageType.ENTITY, PageType.PROJECT, PageType.CONCEPT}


class EntityMergeReport(BaseModel):
    """Summary of one entity merge operation."""

    model_config = ConfigDict(extra="forbid")

    slug_a: str
    slug_b: str
    canonical: str
    loser: str
    aliases_added: list[str] = Field(default_factory=list)
    facts_updated: int = 0
    backlinks_rebuilt: int = 0
    tier_proposals_updated: int = 0
    embeddings_deleted: int = 0
    pages_touched: list[str] = Field(default_factory=list)
    index_rebuilt: bool = False
    committed: bool = False


@coordinated(write=True)
def merge_entities(
    brain_root: Path,
    slug_a: str,
    slug_b: str,
    *,
    into: MergeDirection = "a",
    auto_commit: bool | None = None,
) -> EntityMergeReport:
    """Merge two entity slugs into one canonical entity."""
    if slug_a == slug_b:
        raise BrainError("Cannot merge an entity into itself")
    if into not in {"a", "b"}:
        raise BrainError("into must be 'a' or 'b'")

    canonical = slug_a if into == "a" else slug_b
    loser = slug_b if into == "a" else slug_a
    paths = BrainPaths(Path(brain_root))
    canonical_path, canonical_page = _resolve_unique_page(paths, canonical)
    loser_path, loser_page = _resolve_unique_page(paths, loser)
    for slug, page in ((canonical, canonical_page), (loser, loser_page)):
        if page.frontmatter.type not in _MERGEABLE_TYPES:
            raise BrainError(f"Page is not an entity, project or concept page: {slug}")

    conn = connect(paths.db_path)
    try:
        canonical_row = _require_entity(conn, canonical)
        loser_row = _require_entity(conn, loser)
        alias_transfers = _collect_alias_transfers(
            conn,
            canonical=canonical,
            loser=loser,
            loser_page=loser_page,
            loser_title=str(loser_row["title"]),
        )
        _validate_alias_transfers(conn, alias_transfers, canonical=canonical, loser=loser)

        merged_timeline = _merge_timeline(canonical_page.timeline, loser_page.timeline)
        # Preserve both authors' text. Summary refresh is a separate reviewed operation.
        merged_truth, merged_summary_hash = _merge_truth(canonical_page, loser_page)

        now = _now_utc()
        report = EntityMergeReport(
            slug_a=slug_a,
            slug_b=slug_b,
            canonical=canonical,
            loser=loser,
        )
        merged_page = _merge_page(
            canonical_page=canonical_page,
            loser_page=loser_page,
            aliases=[alias for alias, _ in alias_transfers],
            timeline=merged_timeline,
            compiled_truth=merged_truth,
            summary_hash=merged_summary_hash,
            updated=now,
        )

        page_snapshot = _snapshot_files(
            [*_canonical_page_paths(paths), paths.pages_index]
        )
        try:
            write_page(canonical_path, merged_page)
            loser_path.unlink()
            report.pages_touched.extend(
                [
                    canonical_path.relative_to(paths.root).as_posix(),
                    loser_path.relative_to(paths.root).as_posix(),
                ]
            )
            report.pages_touched.extend(_rewrite_wikilinks(paths, loser=loser, canonical=canonical))
            regenerate_index(paths.root)
            report.index_rebuilt = True
            report.pages_touched.append(paths.pages_index.relative_to(paths.root).as_posix())
        except Exception:
            _restore_files(page_snapshot)
            raise

        try:
            with conn:
                _apply_db_merge(
                    conn,
                    canonical=canonical,
                    loser=loser,
                    canonical_row=canonical_row,
                    loser_row=loser_row,
                    alias_transfers=alias_transfers,
                    canonical_path=canonical_path,
                    paths=paths,
                    now=now,
                    report=report,
                )
                # The loser's facts now belong to the canonical page; a machine-owned
                # summary follows them, while edited or curated text stays untouched.
                refresh_generated_summary(
                    conn, canonical_path, canonical, output_language=_output_language(paths)
                )
                report.backlinks_rebuilt = _replace_all_backlinks(conn, paths)
        except sqlite3.Error as exc:
            _restore_files(page_snapshot)
            raise DBError("Could not merge entity rows") from exc
        except Exception:
            _restore_files(page_snapshot)
            raise
        _finalize_db(conn, paths.db_path)
    finally:
        conn.close()

    report.pages_touched = sorted(set(report.pages_touched))
    report.aliases_added = sorted(set(report.aliases_added))
    _record_merge_event(paths, report)
    append_log(
        paths.root,
        f"- {_now_utc().strftime('%Y-%m-%d %H:%M')} entity merge: {loser} -> {canonical}",
    )
    report.committed = _maybe_commit(
        paths,
        auto_commit,
        f"entity merge: {slug_a} + {slug_b} -> {canonical}",
        [
            paths.db_path,
            paths.events_jsonl,
            paths.pages_log,
            *[paths.root / touched for touched in report.pages_touched],
        ],
    )
    return report


def _merge_truth(canonical: Page, loser: Page) -> tuple[str, str | None]:
    """Keep real text from both sides; a generated placeholder carries nothing to keep."""
    kept = [page for page in (canonical, loser) if page.compiled_truth.strip() not in {"", STUB}]
    if not kept:
        return canonical.compiled_truth, canonical.frontmatter.summary_hash
    if len(kept) == 1 or kept[0].compiled_truth == kept[1].compiled_truth:
        # One side's text survives unchanged, so its ownership marker still describes it.
        return kept[0].compiled_truth, kept[0].frontmatter.summary_hash
    return kept[0].compiled_truth + "\n\n" + kept[1].compiled_truth, None


def _output_language(paths: BrainPaths) -> str:
    try:
        return load_config(paths.config_path).ingest.output_language
    except ConfigError:
        return default_pipeline_config().ingest.output_language


def _record_merge_event(paths: BrainPaths, report: EntityMergeReport) -> None:
    append_event(
        paths.events_jsonl,
        Event(
            id=str(ulid.ULID()),
            timestamp=_now_utc(),
            kind=EventKind.PAGE_EDITED,
            source_ref=f"entity_merge:{report.loser}->{report.canonical}",
            affected_pages=report.pages_touched,
            metadata={
                "action": "entity_merge",
                "canonical": report.canonical,
                "loser": report.loser,
                "facts_updated": report.facts_updated,
                "aliases_added": report.aliases_added,
                "embeddings_deleted": report.embeddings_deleted,
            },
        ),
    )


def _apply_db_merge(
    conn: sqlite3.Connection,
    *,
    canonical: str,
    loser: str,
    canonical_row: sqlite3.Row,
    loser_row: sqlite3.Row,
    alias_transfers: list[tuple[str, str]],
    canonical_path: Path,
    paths: BrainPaths,
    now: datetime,
    report: EntityMergeReport,
) -> None:
    for alias, source in alias_transfers:
        owner = _alias_owner(conn, alias)
        if owner == canonical:
            continue
        conn.execute("DELETE FROM entity_aliases WHERE alias = ? AND entity_id = ?", (alias, loser))
        conn.execute(
            "INSERT INTO entity_aliases (alias, entity_id, source) VALUES (?, ?, ?)",
            (alias, canonical, source),
        )
        report.aliases_added.append(alias)

    report.facts_updated += _execute_count(
        conn,
        "UPDATE facts SET subject = ? WHERE subject = ?",
        (canonical, loser),
    )
    report.facts_updated += _execute_count(
        conn,
        "UPDATE facts SET object = ? WHERE object_type = 'entity' AND object = ?",
        (canonical, loser),
    )

    _merge_backlink_rows(conn, canonical=canonical, loser=loser)
    report.tier_proposals_updated = _execute_count(
        conn,
        "UPDATE tier_proposals SET entity_id = ? WHERE entity_id = ? AND decision IS NULL",
        (canonical, loser),
    )
    # A decided proposal judged the loser; moving it would give the kept entity a
    # rejection cooldown it never had. The decision stays in the review archive and ledger.
    conn.execute("DELETE FROM tier_proposals WHERE entity_id = ?", (loser,))
    # The loser page is deleted; its chunks would otherwise surface as orphaned hits.
    report.embeddings_deleted = delete_page_embeddings(conn, loser)

    first_seen = min(_parse_datetime(canonical_row["first_seen"]), _parse_datetime(loser_row["first_seen"]))
    last_seen = max(now, _parse_datetime(canonical_row["last_seen"]), _parse_datetime(loser_row["last_seen"]))
    tier = min(int(canonical_row["tier"]), int(loser_row["tier"]))
    conn.execute(
        """
        UPDATE entities
        SET tier = ?,
            page_path = ?,
            first_seen = ?,
            last_seen = ?
        WHERE id = ?
        """,
        (
            tier,
            canonical_path.relative_to(paths.root).as_posix(),
            first_seen.isoformat(),
            last_seen.isoformat(),
            canonical,
        ),
    )
    conn.execute("DELETE FROM entities WHERE id = ?", (loser,))
    _update_mention_counts(conn)


def _merge_page(
    *,
    canonical_page: Page,
    loser_page: Page,
    aliases: list[str],
    timeline: list[str],
    compiled_truth: str,
    summary_hash: str | None,
    updated: datetime,
) -> Page:
    frontmatter = canonical_page.frontmatter
    merged_aliases = _unique([*frontmatter.aliases, *aliases])
    merged_sources = _unique([*canonical_page.sources, *loser_page.sources])
    tier_values = [
        int(value)
        for value in (frontmatter.tier, loser_page.frontmatter.tier)
        if value is not None
    ]
    tier = Tier(min(tier_values)) if tier_values else frontmatter.tier
    return canonical_page.model_copy(
        update={
            "frontmatter": frontmatter.model_copy(
                update={
                    "aliases": merged_aliases,
                    "tier": tier,
                    "summary_hash": summary_hash,
                    "updated": updated,
                }
            ),
            "compiled_truth": compiled_truth,
            "timeline": timeline,
            "sources": merged_sources,
        }
    )


def _collect_alias_transfers(
    conn: sqlite3.Connection,
    *,
    canonical: str,
    loser: str,
    loser_page: Page,
    loser_title: str,
) -> list[tuple[str, str]]:
    transfers: list[tuple[str, str]] = []
    rows = conn.execute(
        "SELECT alias, source FROM entity_aliases WHERE entity_id = ? ORDER BY alias",
        (loser,),
    ).fetchall()
    transfers.extend((str(row["alias"]), str(row["source"])) for row in rows)
    transfers.extend(
        (alias, EntityAliasSource.FRONTMATTER.value)
        for alias in loser_page.frontmatter.aliases
    )
    transfers.append((loser_title, EntityAliasSource.MANUAL.value))
    transfers.append((loser, EntityAliasSource.MANUAL.value))

    unique: dict[str, str] = {}
    for alias, source in transfers:
        normalized = alias.strip()
        if normalized and normalized not in {canonical}:
            unique.setdefault(normalized, source)
    return [(alias, source) for alias, source in unique.items()]


def _validate_alias_transfers(
    conn: sqlite3.Connection,
    alias_transfers: list[tuple[str, str]],
    *,
    canonical: str,
    loser: str,
) -> None:
    for alias, _ in alias_transfers:
        owner = _alias_owner(conn, alias)
        if owner is not None and owner not in {canonical, loser}:
            raise BrainError(f"Alias {alias!r} belongs to another entity: {owner}")


def _merge_backlink_rows(conn: sqlite3.Connection, *, canonical: str, loser: str) -> None:
    rows = conn.execute(
        "SELECT from_page, relation, line_number, extracted_at FROM backlinks WHERE to_entity = ?",
        (loser,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO backlinks (from_page, to_entity, relation, line_number, extracted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["from_page"], canonical, row["relation"], row["line_number"], row["extracted_at"]),
        )
    conn.execute("DELETE FROM backlinks WHERE to_entity = ?", (loser,))


def _rebuild_backlinks(paths: BrainPaths) -> int:
    pages = [(path, parse_page(path)) for path in _canonical_page_paths(paths)]
    conn = connect(paths.db_path)
    try:
        with conn:
            conn.execute("DELETE FROM backlinks")
            rebuilt = _replace_backlinks_for_pages(conn, pages)
        _finalize_db(conn, paths.db_path)
        return rebuilt
    except sqlite3.Error as exc:
        raise DBError("Could not rebuild backlinks after merge") from exc
    finally:
        conn.close()


def _replace_all_backlinks(conn: sqlite3.Connection, paths: BrainPaths) -> int:
    pages = [(path, parse_page(path)) for path in _canonical_page_paths(paths)]
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
        filtered = [
            link.model_copy(update={"extracted_at": extracted_at})
            for link in links
            if link.to_entity in entity_types
        ]
        replace_backlinks_for_page(conn, page.frontmatter.slug, filtered)
        rebuilt += len(filtered)
    _update_mention_counts(conn)
    return rebuilt


def _load_alias_map(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, EntityType]]:
    alias_rows = conn.execute("SELECT alias, entity_id FROM entity_aliases").fetchall()
    entity_rows = conn.execute("SELECT id, title, type FROM entities").fetchall()

    alias_map = {str(row["alias"]): str(row["entity_id"]) for row in alias_rows}
    entity_types: dict[str, EntityType] = {}
    for row in entity_rows:
        entity_id = str(row["id"])
        alias_map.setdefault(str(row["title"]), entity_id)
        alias_map.setdefault(entity_id, entity_id)
        entity_types[entity_id] = EntityType(row["type"])
    return alias_map, entity_types


def _rewrite_wikilinks(paths: BrainPaths, *, loser: str, canonical: str) -> list[str]:
    touched: list[str] = []
    for path in _canonical_page_paths(paths):
        original = path.read_text(encoding="utf-8")
        updated = _WIKILINK_PATTERN.sub(
            lambda match: _replace_wikilink(match, loser=loser, canonical=canonical),
            original,
        )
        if updated == original:
            continue
        path.write_text(updated, encoding="utf-8", newline="\n")
        touched.append(path.relative_to(paths.root).as_posix())
    return touched


def _replace_wikilink(match: re.Match[str], *, loser: str, canonical: str) -> str:
    target = match.group(1).strip()
    display = match.group(2)
    if target != loser:
        return match.group(0)
    if display is None:
        return f"[[{canonical}]]"
    return f"[[{canonical}|{display}]]"


def _snapshot_files(paths: list[Path]) -> dict[Path, bytes | object]:
    snapshot: dict[Path, bytes | object] = {}
    for path in paths:
        if path in snapshot:
            continue
        snapshot[path] = path.read_bytes() if path.exists() else _MISSING
    return snapshot


def _restore_files(snapshot: dict[Path, bytes | object]) -> None:
    for path, content in snapshot.items():
        if content is _MISSING:
            with suppress(FileNotFoundError):
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        assert isinstance(content, bytes)
        path.write_bytes(content)


def _merge_timeline(canonical_lines: list[str], loser_lines: list[str]) -> list[str]:
    entries = [parse_entry(line) for line in [*canonical_lines, *loser_lines]]
    unique: dict[tuple[str, str], str] = {}
    for entry in entries:
        unique.setdefault((entry.date, entry.event_id), format_entry(entry))
    return [
        unique[key]
        for key in sorted(unique, key=lambda item: (item[0], item[1]))
    ]


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


def _canonical_page_paths(paths: BrainPaths) -> list[Path]:
    if not paths.pages_dir.exists():
        return []
    return sorted(
        path
        for path in paths.pages_dir.glob("**/*.md")
        if path.is_file() and path.name not in {"index.md", "log.md"}
    )


def _require_entity(conn: sqlite3.Connection, entity_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise BrainError(f"Entity not found: {entity_id}")
    return row


def _alias_owner(conn: sqlite3.Connection, alias: str) -> str | None:
    row = conn.execute("SELECT entity_id FROM entity_aliases WHERE alias = ?", (alias,)).fetchone()
    if row is None:
        return None
    return str(row["entity_id"])


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


def _execute_count(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> int:
    cursor = conn.execute(sql, params)
    return cursor.rowcount if cursor.rowcount >= 0 else 0


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        stripped = value.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            result.append(stripped)
    return result


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

    return git_ops.commit(
        paths.root,
        message,
        paths=_stageable_commit_paths(paths.root, commit_paths),
    ) is not None


def _stageable_commit_paths(root: Path, commit_paths: list[Path]) -> list[Path]:
    from git import Repo
    from git.exc import GitCommandError
    from git.exc import GitError as GitPythonError

    try:
        repo = Repo(root, search_parent_directories=False)
    except GitPythonError:
        return [path for path in commit_paths if path.exists()]

    stageable: list[Path] = []
    for path in commit_paths:
        if path.exists():
            stageable.append(path)
            continue
        try:
            repo.git.ls_files("--error-unmatch", _repo_relative_path(root, path))
        except GitCommandError:
            continue
        stageable.append(path)
    return stageable


def _repo_relative_path(root: Path, path: Path) -> str:
    candidate = path if path.is_absolute() else root / path
    return candidate.resolve().relative_to(root.resolve()).as_posix()


def _finalize_db(conn: sqlite3.Connection, db_path: Path) -> None:
    with suppress(sqlite3.Error):
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _now_utc() -> datetime:
    return datetime.now(UTC)
