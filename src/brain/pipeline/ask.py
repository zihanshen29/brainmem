from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from brain.exceptions import BrainError
from brain.models import Page, PageType
from brain.pages import parse_page
from brain.pages.timeline import parse_entry
from brain.paths import BrainPaths

TOKEN_RE = re.compile(r"[a-z0-9]+")
SUMMARY_LIMIT = 200
RECENT_TIMELINE_LIMIT = 3
BACKLINK_BOOST = 1.5
ENTITY_MATCH_BASE_SCORE = 1.0


class AskPageSummary(BaseModel):
    """One retrieved page summary for ask results."""

    model_config = ConfigDict(extra="forbid")

    page_type: PageType
    slug: str
    title: str
    relative_path: str
    score: float
    compiled_truth: str
    recent_timeline: list[str] = Field(default_factory=list)


class AskModeTrace(BaseModel):
    """Optional deterministic trace for ask mode decisions."""

    model_config = ConfigDict(extra="forbid")

    sql: list[dict[str, Any]] = Field(default_factory=list)
    query_tokens: list[str] = Field(default_factory=list)
    matched_entities: list[str] = Field(default_factory=list)
    boosted_pages: list[str] = Field(default_factory=list)
    explain: str | None = None


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


def ask(
    brain_root: Path | str,
    query: str,
    top: int = 5,
    page_type: PageType | str | None = None,
    explain: bool = False,
    show_sql: bool = False,
) -> AskResult:
    """Retrieve relevant canonical pages for a question.

    Retrieval is deterministic and local: markdown pages provide text scores, while
    SQLite is used only for fixed entity/alias and backlink lookups.
    """
    normalized_query = query.strip()
    if not normalized_query:
        raise BrainError("query must not be empty")
    if top < 1:
        raise BrainError("top must be positive")

    paths = BrainPaths(Path(brain_root))
    selected_type = _normalize_page_type(page_type)
    trace = AskModeTrace() if show_sql or explain else None

    candidates = _load_page_candidates(paths, selected_type)
    query_tokens = _tokens(normalized_query)
    if trace is not None:
        trace.query_tokens = query_tokens

    scores = {
        candidate.page.frontmatter.slug: _score_tokens(query_tokens, candidate.raw_markdown)
        for candidate in candidates
    }

    matched_entities: list[str] = []
    boosted_pages: set[str] = set()
    if paths.db_path.exists():
        conn = _connect_readonly(paths.db_path)
        try:
            matched_entities = _matched_entity_ids(
                conn,
                normalized_query,
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
        finally:
            conn.close()

    if trace is not None:
        trace.matched_entities = matched_entities
        trace.boosted_pages = sorted(boosted_pages)

    summaries = _summaries(candidates, scores, top)
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
    )


def _load_page_candidates(
    paths: BrainPaths,
    page_type: PageType | None,
) -> list[_PageCandidate]:
    if not paths.pages_dir.exists():
        return []

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
    return candidates


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
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _relative(paths: BrainPaths, path: Path) -> str:
    with suppress(ValueError):
        return path.relative_to(paths.root).as_posix()
    return path.as_posix()
