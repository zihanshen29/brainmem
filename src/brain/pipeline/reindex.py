from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import ulid

from brain import git_ops
from brain.concurrency import operation_lock, root_lock
from brain.config import load_config
from brain.config_context import configured_path
from brain.db.connection import connect
from brain.db.embeddings import delete_embedding, find_embeddings_for_page, upsert_embedding
from brain.db.stats import increment_stat, set_stat
from brain.exceptions import EmbeddingError
from brain.ledger.writer import append_event
from brain.llm.embedding import OpenAICompatibleEmbeddingClient
from brain.models import EmbeddingChunk, EmbeddingRecord, Event, EventKind
from brain.pages import parse_page
from brain.paths import BrainPaths
from brain.pipeline.chunking import embedding_content_hash as _content_hash
from brain.pipeline.chunking import split_page_into_chunks
from brain.privacy import external_allowed

_EMBEDDING_SCHEMA_DIMENSION_RE = re.compile(r"\bembedding\s+float\[(\d+)\]", re.IGNORECASE)


@dataclass
class ReindexReport:
    """Summary of one reindex run."""

    pages_scanned: int = 0
    chunks_added: int = 0
    chunks_updated: int = 0
    chunks_removed: int = 0
    chunks_unchanged: int = 0
    tokens_used: int = 0
    dry_run: bool = False
    committed: bool = False
    errors: list[str] = field(default_factory=list)
    pages_private: int = 0

    @property
    def would_embed(self) -> int:
        """Return the number of chunks selected for embedding."""
        return self.chunks_added + self.chunks_updated


@dataclass(frozen=True)
class _PendingChunk:
    chunk: EmbeddingChunk
    content_hash: str
    action: str


def reindex(
    brain_root: Path,
    force: bool = False,
    page_filter: str | Iterable[str] | None = None,
    dry_run: bool = False,
    no_commit: bool = True,
) -> ReindexReport:
    """Incrementally embed page chunks into the vector index."""
    with operation_lock(Path(brain_root), "reindex"), configured_path(Path(brain_root) / "config.toml"):
        return _reindex(brain_root, force, page_filter, dry_run, no_commit)


def _reindex(brain_root: Path, force: bool, page_filter: str | Iterable[str] | None,
             dry_run: bool, no_commit: bool) -> ReindexReport:
    paths = BrainPaths(brain_root)
    config = load_config(paths.config_path)
    report = ReindexReport(dry_run=dry_run)
    filters = _normalize_page_filter(page_filter)

    import hashlib
    snapshots = {}
    with root_lock(paths.root), closing(connect(paths.db_path, read_only=True)) as conn:
        pending: list[_PendingChunk] = []
        orphans: list[EmbeddingRecord] = []
        current_page_slugs: set[str] = set()

        for page_path in _iter_page_paths(paths.pages_dir):
            page = parse_page(page_path)
            slug = page.frontmatter.slug
            current_page_slugs.add(slug)
            if filters is not None and slug not in filters:
                continue

            report.pages_scanned += 1
            snapshots[page_path] = hashlib.sha256(page_path.read_bytes()).hexdigest()
            allowed = external_allowed(paths.root, path=page_path)
            if not allowed:
                report.pages_private += 1
            chunks = split_page_into_chunks(page, config.embedding.chunk_max_chars) if allowed else []
            existing = find_embeddings_for_page(conn, slug)
            existing_by_key = {
                (record.chunk_kind, record.chunk_id): record for record in existing
            }
            new_keys = {(chunk.chunk_kind, chunk.chunk_id) for chunk in chunks}
            orphans.extend(
                record
                for record in existing
                if (record.chunk_kind, record.chunk_id) not in new_keys
            )

            for chunk in chunks:
                content_hash = _content_hash(
                    chunk.text,
                    model=config.embedding.model,
                    dimension=config.embedding.dimension,
                )
                key = (chunk.chunk_kind, chunk.chunk_id)
                existing_record = existing_by_key.get(key)
                if (
                    existing_record is not None
                    and existing_record.content_hash == content_hash
                    and not force
                ):
                    report.chunks_unchanged += 1
                    continue

                action = "added" if existing_record is None else "updated"
                pending.append(_PendingChunk(chunk=chunk, content_hash=content_hash, action=action))

        orphans.extend(_missing_page_orphans(conn, current_page_slugs, filters))

        if dry_run:
            report.chunks_added = sum(item.action == "added" for item in pending)
            report.chunks_updated = sum(item.action == "updated" for item in pending)
            report.chunks_removed = len(orphans)
            return report

        if pending:
            _validate_embedding_schema_dimension(conn, config.embedding.dimension)
        config_bytes = paths.config_path.read_bytes()
    # Provider work has no live write transaction or data-root lock.
    collected = []
    if pending:
        client = OpenAICompatibleEmbeddingClient(config.embedding)
        for start in range(0, len(pending), config.embedding.batch_size):
            batch = pending[start:start + config.embedding.batch_size]
            try:
                vectors = client.embed([item.chunk.text for item in batch])
                collected.extend(list(zip(batch, vectors, strict=True)))
                report.tokens_used += client.last_call_tokens
            except Exception as exc:
                report.errors.append(f"Batch {start // config.embedding.batch_size + 1} failed: {type(exc).__name__}")
                for item in batch:
                    try:
                        vectors = client.embed([item.chunk.text])
                        collected.append((item, vectors[0]))
                        report.tokens_used += client.last_call_tokens
                    except Exception as individual_exc:
                        report.errors.append(f"{item.chunk.page_slug}: {type(individual_exc).__name__}")
    with root_lock(paths.root, write=True), closing(connect(paths.db_path)) as conn:
        if config_bytes != paths.config_path.read_bytes() or any(
            not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            for path, digest in snapshots.items()
        ):
            raise EmbeddingError("Pages or configuration changed during embedding; rerun reindex")
        for item, vector in collected:
            path = next(path for path in snapshots if parse_page(path).frontmatter.slug == item.chunk.page_slug)
            if not external_allowed(paths.root, path=path):
                report.errors.append(f"{item.chunk.page_slug}: now local-only; embedding discarded")
                continue
            _upsert_successful(conn, [item], [vector], config.embedding.model, report)
        with conn:
            for orphan in orphans:
                delete_embedding(conn, orphan.rowid)
            report.chunks_removed = len(orphans)

        if report.chunks_added or report.chunks_updated or report.chunks_removed:
            _write_event(paths, report, config.embedding.model)
            _update_stats(conn, report.tokens_used, config.embedding.unit_cost_per_1m_tokens)

    with root_lock(paths.root, write=True):
        if not no_commit:
            report.committed = (
                git_ops.commit(
                    paths.root,
                    "reindex: update embedding index",
                    paths=[paths.db_path, paths.events_jsonl],
                )
                is not None
            )

    return report


def _iter_page_paths(pages_dir: Path) -> list[Path]:
    paths = [
        path
        for path in pages_dir.rglob("*.md")
        if path.name not in {"index.md", "log.md"}
    ]
    return sorted(paths, key=lambda path: path.relative_to(pages_dir).as_posix())


def _normalize_page_filter(page_filter: str | Iterable[str] | None) -> set[str] | None:
    if page_filter is None:
        return None
    values = [page_filter] if isinstance(page_filter, str) else list(page_filter)
    filters = {value.strip() for value in values if value.strip()}
    return filters or None


def _missing_page_orphans(conn, current_page_slugs: set[str], filters: set[str] | None) -> list[EmbeddingRecord]:
    indexed_slugs = _indexed_page_slugs(conn)
    stale_slugs = indexed_slugs - current_page_slugs if filters is None else filters - current_page_slugs
    return [
        record
        for slug in sorted(stale_slugs)
        for record in find_embeddings_for_page(conn, slug)
    ]


def _indexed_page_slugs(conn) -> set[str]:
    rows = conn.execute("SELECT DISTINCT page_slug FROM embedding_index").fetchall()
    return {row["page_slug"] for row in rows}


def _validate_embedding_schema_dimension(conn: sqlite3.Connection, config_dimension: int) -> None:
    schema_dimension = _embedding_schema_dimension(conn)
    if schema_dimension == config_dimension:
        return
    raise EmbeddingError(
        "Embedding dimension mismatch: sqlite-vec embeddings table expects "
        f"float[{schema_dimension}], but config.toml has embedding.dimension = "
        f"{config_dimension}. Update config.toml to match the existing vector "
        "index, or recreate the index before running mem reindex."
    )


def _embedding_schema_dimension(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'embeddings'
        """
    ).fetchone()
    schema_sql = row["sql"] if row is not None else None
    if not schema_sql:
        raise EmbeddingError("Could not find sqlite-vec embeddings table schema")

    match = _EMBEDDING_SCHEMA_DIMENSION_RE.search(schema_sql)
    if match is None:
        raise EmbeddingError(
            "Could not determine sqlite-vec embedding dimension from embeddings schema"
        )
    return int(match.group(1))


def _embed_pending(
    conn,
    pending: list[_PendingChunk],
    client: OpenAICompatibleEmbeddingClient,
    model: str,
    batch_size: int,
    report: ReindexReport,
) -> None:
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        try:
            vectors = client.embed([item.chunk.text for item in batch])
        except Exception as exc:
            report.errors.append(f"Batch {start // batch_size + 1} failed: {exc}")
            _embed_individual(conn, batch, client, model, report)
            continue

        report.tokens_used += client.last_call_tokens
        _upsert_successful(conn, batch, vectors, model, report)


def _embed_individual(
    conn,
    batch: list[_PendingChunk],
    client: OpenAICompatibleEmbeddingClient,
    model: str,
    report: ReindexReport,
) -> None:
    for item in batch:
        try:
            vectors = client.embed([item.chunk.text])
        except Exception as exc:
            report.errors.append(
                f"{item.chunk.page_slug}/{item.chunk.chunk_kind}/{item.chunk.chunk_id}: {exc}"
            )
            continue
        report.tokens_used += client.last_call_tokens
        _upsert_successful(conn, [item], vectors, model, report)


def _upsert_successful(
    conn,
    batch: list[_PendingChunk],
    vectors: list[list[float]],
    model: str,
    report: ReindexReport,
) -> None:
    with conn:
        for item, vector in zip(batch, vectors, strict=True):
            upsert_embedding(conn, item.chunk, item.content_hash, vector, model)
            if item.action == "added":
                report.chunks_added += 1
            else:
                report.chunks_updated += 1


def _write_event(paths: BrainPaths, report: ReindexReport, model: str) -> None:
    append_event(
        paths.events_jsonl,
        Event(
            id=str(ulid.ULID()),
            timestamp=datetime.now(UTC),
            kind=EventKind.REINDEXED,
            source_ref="reindex",
            affected_pages=[],
            metadata={
                "chunks_added": report.chunks_added,
                "chunks_updated": report.chunks_updated,
                "chunks_removed": report.chunks_removed,
                "model": model,
                "tokens_used": report.tokens_used,
            },
        ),
    )


def _update_stats(conn, tokens_used: int, unit_cost_per_1m_tokens: float) -> None:
    with conn:
        set_stat(conn, "last_reindex_at", datetime.now(UTC).isoformat())
        if tokens_used:
            increment_stat(conn, "total_embedding_tokens", tokens_used)
            increment_stat(
                conn,
                "total_cost_usd",
                tokens_used / 1_000_000 * unit_cost_per_1m_tokens,
            )
