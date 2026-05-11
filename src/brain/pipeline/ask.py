from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from brain.config import EmbeddingConfig, load_config
from brain.db.connection import sqlite_uri
from brain.exceptions import BrainError
from brain.models import (
    EmbeddingChunk,
    Frontmatter,
    FusedResult,
    Page,
    PageType,
    ProcedureStatus,
    RetrievalHit,
)
from brain.pages import parse_page
from brain.pages.timeline import parse_entry
from brain.paths import BrainPaths

TOKEN_RE = re.compile(r"[a-z0-9]+")
SUMMARY_LIMIT = 200
RECENT_TIMELINE_LIMIT = 3
BACKLINK_BOOST = 1.5
ENTITY_MATCH_BASE_SCORE = 1.0
AskMode = Literal["hybrid", "keyword-only", "semantic", "sql"]


class AskPageSummary(BaseModel):
    """One retrieved page summary for ask results."""

    model_config = ConfigDict(extra="forbid")

    page_type: PageType | str
    slug: str
    title: str
    relative_path: str
    score: float
    compiled_truth: str
    recent_timeline: list[str] = Field(default_factory=list)
    debug: dict[str, Any] = Field(default_factory=dict)


class AskModeTrace(BaseModel):
    """Optional deterministic trace for ask mode decisions."""

    model_config = ConfigDict(extra="forbid")

    sql: list[dict[str, Any]] = Field(default_factory=list)
    query_tokens: list[str] = Field(default_factory=list)
    matched_entities: list[str] = Field(default_factory=list)
    boosted_pages: list[str] = Field(default_factory=list)
    explain: str | None = None
    mode: str | None = None
    effective_mode: str | None = None
    classifier: str | None = None
    warnings: list[str] = Field(default_factory=list)
    vector: list[dict[str, Any]] = Field(default_factory=list)
    keyword: list[dict[str, Any]] = Field(default_factory=list)
    sql_path: list[dict[str, Any]] = Field(default_factory=list)
    rrf: list[dict[str, Any]] = Field(default_factory=list)


class AskResult(BaseModel):
    """Result returned by the Ask retrieval pipeline."""

    model_config = ConfigDict(extra="forbid")

    query: str
    top: int
    page_type: PageType | None = None
    results: list[AskPageSummary] = Field(default_factory=list)
    answer: str | None = None
    sources: list[str] = Field(default_factory=list)
    trace: AskModeTrace | None = None
    mode: str = "keyword-only"
    effective_mode: str = "keyword-only"
    warnings: list[str] = Field(default_factory=list)

    @property
    def pages(self) -> list[AskPageSummary]:
        """Compatibility alias for callers that use page terminology."""
        return self.results


class _PageCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    page: Page
    raw_markdown: str
    relative_path: str
    marker: str = "page"


def ask(
    brain_root: Path | str,
    query: str,
    top: int = 5,
    page_type: PageType | str | None = None,
    explain: bool = False,
    show_sql: bool = False,
    mode: AskMode | str | None = None,
    debug: bool = False,
) -> AskResult:
    """Retrieve relevant canonical pages for a question."""
    normalized_query = query.strip()
    if not normalized_query:
        raise BrainError("query must not be empty")
    if top < 1:
        raise BrainError("top must be positive")

    paths = BrainPaths(Path(brain_root))
    selected_type = _normalize_page_type(page_type)
    config = load_config(paths.config_path)
    requested_mode = _normalize_mode(mode or config.retrieval.default_mode)
    trace = AskModeTrace(mode=requested_mode) if show_sql or explain or debug else None
    classifier = _classify_query(normalized_query)
    if trace is not None:
        trace.classifier = classifier

    candidates = _load_page_candidates(paths, selected_type)
    query_tokens = _tokens(normalized_query)
    if trace is not None:
        trace.query_tokens = query_tokens

    warnings: list[str] = []
    effective_mode = requested_mode
    conn = _connect_optional(paths.db_path)
    try:
        fused: list[FusedResult] = []
        if (
            requested_mode == "hybrid"
            and classifier == "structured"
            and config.retrieval.sql_shortcut_enabled
        ):
            fused = _sql_results(conn, normalized_query, config.retrieval.final_top, trace)
            if fused:
                effective_mode = "sql"

        if requested_mode == "hybrid" and effective_mode == "hybrid" and not _vector_path_available(conn):
            effective_mode = "keyword-only"
            warnings.append(
                "No usable embeddings found. Falling back to keyword-only mode. "
                "Run `mem reindex` to enable hybrid retrieval."
            )

        if fused:
            pass
        elif effective_mode == "semantic":
            fused = _semantic_results(
                conn,
                normalized_query,
                config.retrieval.top_per_path,
                config.embedding,
                trace,
            )
        elif effective_mode == "sql":
            fused = _sql_results(conn, normalized_query, config.retrieval.final_top, trace)
        elif effective_mode == "hybrid":
            fused = _hybrid_results(
                conn,
                normalized_query,
                query_tokens,
                candidates,
                config.retrieval.top_per_path,
                config.retrieval.rrf_k,
                config.embedding,
                show_sql,
                trace,
            )
        else:
            fused = _keyword_only_results(
                conn,
                normalized_query,
                query_tokens,
                candidates,
                show_sql,
                trace,
            )
    except _VectorUnavailable as exc:
        if requested_mode == "hybrid":
            effective_mode = "keyword-only"
            warnings.append(
                f"Vector retrieval unavailable ({exc}). Falling back to keyword-only mode. "
                "Run `mem reindex` to enable hybrid retrieval."
            )
            fused = _keyword_only_results(
                conn,
                normalized_query,
                query_tokens,
                candidates,
                show_sql,
                trace,
            )
        else:
            warnings.append(f"Vector retrieval unavailable: {exc}")
            fused = []
    finally:
        if conn is not None:
            conn.close()

    fused = _filter_fused_to_candidates(candidates, fused)
    fused = _apply_candidate_weights(candidates, fused)
    if trace is not None:
        _trace_fused(trace, fused)
        trace.effective_mode = effective_mode
        trace.warnings = warnings

    summaries = _summaries_from_fused(candidates, fused[:top])
    answer: str | None = None
    sources: list[str] = []
    if explain:
        answer, sources, explain_note = _answer_question(normalized_query, summaries)
        if trace is not None:
            trace.explain = explain_note

    return AskResult(
        query=normalized_query,
        top=top,
        page_type=selected_type,
        results=summaries,
        answer=answer,
        sources=sources,
        trace=trace,
        mode=requested_mode,
        effective_mode=effective_mode,
        warnings=warnings,
    )


class _VectorUnavailable(Exception):
    """Raised when semantic retrieval cannot be used."""


def _normalize_mode(mode: str) -> AskMode:
    if mode not in {"hybrid", "keyword-only", "semantic", "sql"}:
        raise BrainError(f"unsupported ask mode: {mode}")
    return mode  # type: ignore[return-value]


def _classify_query(query: str) -> str:
    from brain.pipeline.retrieval.classifier import classify_query

    return classify_query(query)


def _hybrid_results(
    conn: sqlite3.Connection | None,
    query: str,
    query_tokens: list[str],
    candidates: list[_PageCandidate],
    top_per_path: int,
    rrf_k: int,
    embedding_config: EmbeddingConfig,
    show_sql: bool,
    trace: AskModeTrace | None,
) -> list[FusedResult]:
    vector_hits = _vector_hits(conn, query, top_per_path, embedding_config)
    keyword_hits = _keyword_hits(query, query_tokens, candidates, top_per_path)
    sql_hits = _sql_hits(conn, query, query_tokens, candidates, show_sql, trace, top_per_path)
    fused = _rrf_fuse(vector_hits, keyword_hits, sql_hits, k=rrf_k)
    _trace_hits(trace, "vector", vector_hits)
    _trace_hits(trace, "keyword", keyword_hits)
    _trace_hits(trace, "sql", sql_hits)
    _trace_fused(trace, fused)
    return fused


def _semantic_results(
    conn: sqlite3.Connection | None,
    query: str,
    top_per_path: int,
    embedding_config: EmbeddingConfig,
    trace: AskModeTrace | None,
) -> list[FusedResult]:
    vector_hits = _vector_hits(conn, query, top_per_path, embedding_config)
    fused = _rrf_fuse(vector_hits, k=1)
    _trace_hits(trace, "vector", vector_hits)
    _trace_fused(trace, fused)
    return fused


def _sql_results(
    conn: sqlite3.Connection | None,
    query: str,
    top: int,
    trace: AskModeTrace | None,
) -> list[FusedResult]:
    if conn is None:
        return []
    try:
        from brain.pipeline.retrieval.sql_direct import sql_direct_query

        fused = list(sql_direct_query(conn, query, top))
    except ImportError:
        fused = []
    _trace_fused(trace, fused)
    _trace_hits(trace, "sql", [hit for result in fused for hit in result.chunks])
    return fused


def _keyword_only_results(
    conn: sqlite3.Connection | None,
    query: str,
    query_tokens: list[str],
    candidates: list[_PageCandidate],
    show_sql: bool,
    trace: AskModeTrace | None,
) -> list[FusedResult]:
    scores = {
        candidate.page.frontmatter.slug: _score_tokens(query_tokens, candidate.raw_markdown)
        for candidate in candidates
    }
    matched_entities: list[str] = []
    boosted_pages: set[str] = set()
    if conn is not None:
        matched_entities = _matched_entity_ids(
            conn,
            query,
            query_tokens,
            trace if show_sql else None,
        )
        _boost_entity_pages(
            matched_entities,
            scores,
            {candidate.page.frontmatter.slug for candidate in candidates},
        )
        boosted_pages = _boost_backlink_sources(
            conn,
            matched_entities,
            scores,
            {candidate.page.frontmatter.slug for candidate in candidates},
            trace if show_sql else None,
        )

    if trace is not None:
        trace.matched_entities = matched_entities
        trace.boosted_pages = sorted(boosted_pages)

    ranked = sorted(
        [candidate for candidate in candidates if scores.get(candidate.page.frontmatter.slug, 0.0) > 0],
        key=lambda candidate: (
            -scores.get(candidate.page.frontmatter.slug, 0.0),
            candidate.relative_path,
        ),
    )
    hits = [
        RetrievalHit(
            page_slug=candidate.page.frontmatter.slug,
            chunk_kind="compiled_truth",
            chunk_id="main",
            score=round(scores[candidate.page.frontmatter.slug], 6),
            rank=index,
            path="keyword",
        )
        for index, candidate in enumerate(ranked, start=1)
    ]
    _trace_hits(trace, "keyword", hits)
    fused = [
        FusedResult(
            page_slug=hit.page_slug,
            chunks=[hit],
            rrf_score=hit.score,
            final_rank=hit.rank,
        )
        for hit in hits
    ]
    _trace_fused(trace, fused)
    return fused


def _keyword_hits(
    query: str,
    query_tokens: list[str],
    candidates: list[_PageCandidate],
    top: int,
) -> list[RetrievalHit]:
    chunks = _chunks_from_candidates(candidates)
    try:
        from brain.pipeline.retrieval.keyword import bm25_search

        return list(bm25_search(chunks, query, top))
    except ImportError:
        pass

    scored = [
        (
            candidate,
            _score_tokens(query_tokens, _chunk_text_from_candidate(candidate)),
        )
        for candidate in candidates
    ]
    ranked = sorted(
        [(candidate, score) for candidate, score in scored if score > 0],
        key=lambda item: (-item[1], item[0].relative_path),
    )
    return [
        RetrievalHit(
            page_slug=candidate.page.frontmatter.slug,
            chunk_kind="compiled_truth",
            chunk_id="main",
            score=round(score, 6),
            rank=index,
            path="keyword",
        )
        for index, (candidate, score) in enumerate(ranked[:top], start=1)
    ]


def _sql_hits(
    conn: sqlite3.Connection | None,
    query: str,
    query_tokens: list[str],
    candidates: list[_PageCandidate],
    show_sql: bool,
    trace: AskModeTrace | None,
    top: int,
) -> list[RetrievalHit]:
    if conn is None:
        return []
    try:
        from brain.pipeline.retrieval.sql_match import sql_entity_match

        hits = list(sql_entity_match(conn, query, top))
        _trace_hits(trace, "sql", hits)
        return hits
    except ImportError:
        pass

    candidate_slugs = {candidate.page.frontmatter.slug for candidate in candidates}
    matched_entities = _matched_entity_ids(conn, query, query_tokens, trace if show_sql else None)
    scores: dict[str, float] = {}
    _boost_entity_pages(matched_entities, scores, candidate_slugs)
    boosted_pages = _boost_backlink_sources(
        conn,
        matched_entities,
        scores,
        candidate_slugs,
        trace if show_sql else None,
    )
    if trace is not None:
        trace.matched_entities = matched_entities
        trace.boosted_pages = sorted(boosted_pages)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [
        RetrievalHit(
            page_slug=slug,
            chunk_kind="compiled_truth",
            chunk_id="main",
            score=score,
            rank=index,
            path="sql",
        )
        for index, (slug, score) in enumerate(ranked[:top], start=1)
    ]


def _vector_hits(
    conn: sqlite3.Connection | None,
    query: str,
    top: int,
    embedding_config: EmbeddingConfig,
) -> list[RetrievalHit]:
    if conn is None:
        raise _VectorUnavailable("database is unavailable")
    try:
        from brain.pipeline.retrieval.vector import vector_search as retrieval_vector_search
    except ImportError as exc:
        raise _VectorUnavailable("retrieval vector module is unavailable") from exc
    try:
        from brain.llm.embedding import OpenAICompatibleEmbeddingClient

        client = OpenAICompatibleEmbeddingClient(embedding_config)
        return list(retrieval_vector_search(conn, query, client, top=top))
    except Exception as exc:
        raise _VectorUnavailable(str(exc)) from exc


def _rrf_fuse(*paths: list[RetrievalHit], k: int) -> list[FusedResult]:
    try:
        from brain.pipeline.retrieval.rrf import rrf_fuse as retrieval_rrf_fuse
    except ImportError:
        retrieval_rrf_fuse = None
    if retrieval_rrf_fuse is not None:
        return list(retrieval_rrf_fuse(*paths, k=k))

    page_scores: dict[str, dict[str, Any]] = {}
    for hits in paths:
        for hit in hits:
            data = page_scores.setdefault(hit.page_slug, {"score": 0.0, "chunks": []})
            data["score"] += 1.0 / (k + hit.rank)
            data["chunks"].append(hit)
    fused = [
        FusedResult(
            page_slug=slug,
            chunks=data["chunks"],
            rrf_score=data["score"],
            final_rank=0,
        )
        for slug, data in page_scores.items()
    ]
    fused.sort(key=lambda result: (-result.rrf_score, result.page_slug))
    for index, result in enumerate(fused, start=1):
        result.final_rank = index
    return fused


def _summaries_from_fused(
    candidates: list[_PageCandidate],
    fused: list[FusedResult],
) -> list[AskPageSummary]:
    candidates_by_slug = {candidate.page.frontmatter.slug: candidate for candidate in candidates}
    summaries: list[AskPageSummary] = []
    for result in fused:
        candidate = candidates_by_slug.get(result.page_slug)
        if candidate is None:
            continue
        page = candidate.page
        debug = {
            "rrf_score": result.rrf_score,
            "final_rank": result.final_rank,
            "paths": {hit.path: hit.rank for hit in result.chunks},
        }
        if candidate.marker != "page":
            debug["marker"] = candidate.marker
        summaries.append(
            AskPageSummary(
                page_type=page.frontmatter.type,
                slug=page.frontmatter.slug,
                title=page.frontmatter.title,
                relative_path=candidate.relative_path,
                score=round(result.rrf_score, 6),
                compiled_truth=page.compiled_truth[:SUMMARY_LIMIT],
                recent_timeline=_recent_timeline(page.timeline),
                debug=debug,
            )
        )
    return summaries


def _chunk_text_from_candidate(candidate: _PageCandidate) -> str:
    page = candidate.page
    timeline_text = "\n".join(page.timeline)
    return f"{page.frontmatter.title}\n\n{page.compiled_truth}\n\n{timeline_text}"


def _chunks_from_candidates(candidates: list[_PageCandidate]) -> list[EmbeddingChunk]:
    return [
        EmbeddingChunk(
            page_slug=candidate.page.frontmatter.slug,
            chunk_kind="compiled_truth",
            chunk_id="scratch/SNAPSHOT.md" if candidate.marker == "scratch/snapshot" else "main",
            text=_chunk_text_from_candidate(candidate),
            text_preview=candidate.page.compiled_truth[:SUMMARY_LIMIT],
        )
        for candidate in candidates
    ]


def _trace_hits(trace: AskModeTrace | None, path: str, hits: list[RetrievalHit]) -> None:
    if trace is None:
        return
    rows = [
        {
            "page_slug": hit.page_slug,
            "chunk_kind": hit.chunk_kind,
            "chunk_id": hit.chunk_id,
            "score": hit.score,
            "rank": hit.rank,
            "path": hit.path,
        }
        for hit in hits[:10]
    ]
    if path == "vector":
        trace.vector = rows
    elif path == "keyword":
        trace.keyword = rows
    elif path == "sql":
        trace.sql_path = rows


def _trace_fused(trace: AskModeTrace | None, fused: list[FusedResult]) -> None:
    if trace is None:
        return
    trace.rrf = [
        {
            "page_slug": result.page_slug,
            "rrf_score": result.rrf_score,
            "final_rank": result.final_rank,
            "paths": {hit.path: hit.rank for hit in result.chunks},
        }
        for result in fused[:10]
    ]


def _apply_candidate_weights(
    candidates: list[_PageCandidate],
    fused: list[FusedResult],
) -> list[FusedResult]:
    weights = {
        candidate.page.frontmatter.slug: _candidate_weight(candidate)
        for candidate in candidates
    }
    adjusted = [
        result.model_copy(update={"rrf_score": result.rrf_score * weights.get(result.page_slug, 1.0)})
        for result in fused
    ]
    adjusted.sort(key=lambda result: (-result.rrf_score, result.page_slug))
    for index, result in enumerate(adjusted, start=1):
        result.final_rank = index
    return adjusted


def _filter_fused_to_candidates(
    candidates: list[_PageCandidate],
    fused: list[FusedResult],
) -> list[FusedResult]:
    candidate_slugs = {candidate.page.frontmatter.slug for candidate in candidates}
    if not candidate_slugs:
        return []
    return [result for result in fused if result.page_slug in candidate_slugs]


def _vector_path_available(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return False
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    try:
        row = conn.execute("SELECT COUNT(*) AS count FROM embedding_index").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and int(row["count"]) > 0


def _load_page_candidates(
    paths: BrainPaths,
    page_type: PageType | None,
) -> list[_PageCandidate]:
    if not paths.pages_dir.exists():
        return _load_scratch_candidates(paths, page_type)

    candidates: list[_PageCandidate] = []
    for page_path in sorted(paths.pages_dir.rglob("*.md")):
        if page_path.name in {"index.md", "log.md"}:
            continue
        with suppress(Exception):
            page = parse_page(page_path)
            if page_type is not None and page.frontmatter.type is not page_type:
                continue
            candidates.append(
                _PageCandidate(
                    path=page_path,
                    page=page,
                    raw_markdown=page_path.read_text(encoding="utf-8"),
                    relative_path=_relative(paths, page_path),
                )
            )
    candidates.extend(_load_scratch_candidates(paths, page_type))
    return candidates


def _load_scratch_candidates(
    paths: BrainPaths,
    page_type: PageType | None,
) -> list[_PageCandidate]:
    return [
        *_load_scratch_file_candidate(
            paths,
            path=paths.snapshot_path,
            slug="scratch-snapshot",
            title="Scratch Snapshot",
            source="scratch/SNAPSHOT.md",
            marker="scratch/snapshot",
            page_type=page_type,
        ),
        *_load_scratch_file_candidate(
            paths,
            path=paths.working_buffer,
            slug="scratch-working",
            title="Scratch Working Buffer",
            source="scratch/working.md",
            marker="scratch/working",
            page_type=page_type,
        ),
    ]


def _load_scratch_file_candidate(
    paths: BrainPaths,
    *,
    path: Path,
    slug: str,
    title: str,
    source: str,
    marker: str,
    page_type: PageType | None,
) -> list[_PageCandidate]:
    if page_type is not None:
        return []
    if not path.exists() or not path.is_file():
        return []
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []
    timestamp = path.stat().st_mtime
    from datetime import UTC, datetime

    updated = datetime.fromtimestamp(timestamp, tz=UTC)
    page = Page(
        frontmatter=Frontmatter(
            type=PageType.CONCEPT,
            slug=slug,
            title=title,
            created=updated,
            updated=updated,
            tags=["scratch", "snapshot"],
            aliases=[title],
            external_ids={},
        ),
        compiled_truth=content,
        timeline=[],
        sources=[source],
    )
    return [
        _PageCandidate(
            path=path,
            page=page,
            raw_markdown=content,
            relative_path=_relative(paths, path),
            marker=marker,
        )
    ]


def _candidate_weight(candidate: _PageCandidate) -> float:
    if candidate.marker == "scratch/snapshot":
        return 0.35
    if candidate.marker == "scratch/working":
        return 0.25
    frontmatter = candidate.page.frontmatter
    if frontmatter.type is not PageType.PROCEDURE:
        return 1.0
    if frontmatter.status is ProcedureStatus.STABLE:
        return 1.0
    if frontmatter.status is ProcedureStatus.TESTED:
        return 0.7
    return 0.3


def _score_tokens(query_tokens: list[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    counts = Counter(_tokens(text))
    score = 0.0
    for token in set(query_tokens):
        count = counts[token]
        if count:
            score += 1.0 + math.log(count)
    return score


def _matched_entity_ids(
    conn: sqlite3.Connection,
    query: str,
    tokens: list[str],
    trace: AskModeTrace | None,
) -> list[str]:
    terms = _entity_terms(query, tokens)

    matches: list[str] = []
    seen: set[str] = set()

    title_sql = "SELECT id FROM entities WHERE title = ? OR lower(title) = lower(?) ORDER BY id"
    for term in terms:
        title_params = (term, term)
        _trace_sql(trace, title_sql, title_params)
        for row in conn.execute(title_sql, title_params).fetchall():
            entity_id = str(row["id"])
            if entity_id not in seen:
                seen.add(entity_id)
                matches.append(entity_id)

    alias_sql = (
        "SELECT entity_id FROM entity_aliases "
        "WHERE alias = ? OR lower(alias) = lower(?) ORDER BY entity_id"
    )
    for term in terms:
        alias_params = (term, term)
        _trace_sql(trace, alias_sql, alias_params)
        for row in conn.execute(alias_sql, alias_params).fetchall():
            entity_id = str(row["entity_id"])
            if entity_id not in seen:
                seen.add(entity_id)
                matches.append(entity_id)

    return matches


def _entity_terms(query: str, tokens: list[str]) -> list[str]:
    return [term for term in dict.fromkeys([query.strip(), query.strip().lower(), *tokens]) if term]


def _boost_entity_pages(
    entity_ids: list[str],
    scores: dict[str, float],
    candidate_slugs: set[str],
) -> None:
    for entity_id in entity_ids:
        if entity_id in candidate_slugs:
            scores[entity_id] = max(scores.get(entity_id, 0.0), ENTITY_MATCH_BASE_SCORE)


def _boost_backlink_sources(
    conn: sqlite3.Connection,
    entity_ids: list[str],
    scores: dict[str, float],
    candidate_slugs: set[str],
    trace: AskModeTrace | None,
) -> set[str]:
    boosted: set[str] = set()
    if not entity_ids:
        return boosted

    sql = "SELECT DISTINCT from_page FROM backlinks WHERE to_entity = ? ORDER BY from_page"
    for entity_id in entity_ids:
        params = (entity_id,)
        _trace_sql(trace, sql, params)
        rows = conn.execute(sql, params).fetchall()
        for row in rows:
            slug = str(row["from_page"])
            if slug not in candidate_slugs:
                continue
            scores[slug] = max(scores.get(slug, 0.0), ENTITY_MATCH_BASE_SCORE) * BACKLINK_BOOST
            boosted.add(slug)
    return boosted


def _summaries(
    candidates: list[_PageCandidate],
    scores: dict[str, float],
    top: int,
) -> list[AskPageSummary]:
    ranked = sorted(
        [candidate for candidate in candidates if scores.get(candidate.page.frontmatter.slug, 0.0) > 0],
        key=lambda candidate: (
            -scores.get(candidate.page.frontmatter.slug, 0.0),
            candidate.relative_path,
        ),
    )
    summaries: list[AskPageSummary] = []
    for candidate in ranked[:top]:
        page = candidate.page
        slug = page.frontmatter.slug
        summaries.append(
            AskPageSummary(
                page_type=page.frontmatter.type,
                slug=slug,
                title=page.frontmatter.title,
                relative_path=candidate.relative_path,
                score=round(scores.get(slug, 0.0), 6),
                compiled_truth=page.compiled_truth[:SUMMARY_LIMIT],
                recent_timeline=_recent_timeline(page.timeline),
            )
        )
    return summaries


def _recent_timeline(lines: list[str]) -> list[str]:
    indexed = []
    for index, line in enumerate(lines):
        entry = parse_entry(line)
        indexed.append((entry.date, index, line))
    return [
        line
        for _, _, line in sorted(indexed, key=lambda item: (item[0], item[1]), reverse=True)[
            :RECENT_TIMELINE_LIMIT
        ]
    ]


def _answer_question(query: str, pages: list[AskPageSummary]) -> tuple[str | None, list[str], str]:
    try:
        from brain.llm import client
    except Exception as exc:  # pragma: no cover - defensive seam for optional LLM stack
        return None, [], f"brain.llm.client could not be imported: {exc}"

    answer_question = getattr(client, "answer_question", None)
    if answer_question is None:
        return None, [], "brain.llm.client.answer_question is not implemented"
    response = answer_question(query, [page.model_dump(mode="json") for page in pages])
    answer = getattr(response, "answer", response)
    sources = getattr(response, "sources", [])
    return str(answer), list(sources), "brain.llm.client.answer_question"


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _normalize_page_type(page_type: PageType | str | None) -> PageType | None:
    if page_type is None:
        return None
    if isinstance(page_type, PageType):
        return page_type
    try:
        return PageType(page_type)
    except ValueError as exc:
        raise BrainError(f"unsupported page type: {page_type}") from exc


def _trace_sql(trace: AskModeTrace | None, sql: str, params: tuple[Any, ...]) -> None:
    if trace is None:
        return
    trace.sql.append({"sql": " ".join(sql.split()), "params": list(params)})


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(sqlite_uri(path, mode="ro"), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_optional(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        from brain.db.connection import connect

        return connect(path)
    except Exception:
        pass
    try:
        return _connect_readonly(path)
    except sqlite3.Error:
        return None


def _relative(paths: BrainPaths, path: Path) -> str:
    with suppress(ValueError):
        return path.relative_to(paths.root).as_posix()
    return path.as_posix()
