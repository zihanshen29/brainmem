from __future__ import annotations

import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from brain.exceptions import BrainError
from brain.models import PageType
from brain.pages import parse_page
from brain.paths import BrainPaths
from brain.pipeline.ask import AskMode, ask

OutputFormat = Literal["markdown", "text"]

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
MIN_TRUNCATED_FRAGMENT_TOKENS = 8


class InjectionFragment(BaseModel):
    """One page fragment selected for prompt injection."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str
    relative_path: str
    token_count: int
    truncated: bool = False


class SkippedFragment(BaseModel):
    """A page skipped because no meaningful content fit the budget."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    relative_path: str
    reason: str


class InjectionResult(BaseModel):
    """Rendered token-aware injection payload."""

    model_config = ConfigDict(extra="forbid")

    query: str
    budget: int
    used_tokens: int
    fragment_count: int
    output_format: OutputFormat
    mode: str
    effective_mode: str
    top: int
    content: str
    fragments: list[InjectionFragment] = Field(default_factory=list)
    skipped: list[SkippedFragment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def inject(
    brain_root: Path | str,
    query: str,
    budget: int = 10000,
    output_format: OutputFormat = "markdown",
    mode: AskMode | str = "keyword-only",
    top: int = 8,
    include_snapshot: bool = True,
    page_type: PageType | str | None = None,
    include_slugs: Sequence[str] | None = None,
    snapshot_path: Path | None = None,
) -> InjectionResult:
    """Build a token-bounded context bundle from retrieved Brain pages."""
    normalized_query = query.strip()
    if not normalized_query:
        raise BrainError("query must not be empty")
    if budget < 1:
        raise BrainError("budget must be positive")
    if top < 1:
        raise BrainError("top must be positive")
    if output_format not in {"markdown", "text"}:
        raise BrainError(f"unsupported injection format: {output_format}")

    paths = BrainPaths(Path(brain_root))
    retrieval = ask(paths.root, normalized_query, top=top, mode=mode, page_type=page_type)
    rendered: list[str] = []
    fragments: list[InjectionFragment] = []
    skipped: list[SkippedFragment] = []
    emitted_slugs: set[str] = set()

    if include_snapshot:
        snapshot_text = _read_snapshot(_resolve_snapshot_path(paths, snapshot_path))
        if snapshot_text is not None:
            _append_budgeted_fragment(
                output_format,
                query=normalized_query,
                budget=budget,
                slug="snapshot",
                title="Current Snapshot",
                relative_path="scratch/SNAPSHOT.md",
                page_type="snapshot",
                compiled_truth=snapshot_text,
                timeline=[],
                sources=[],
                fragments=fragments,
                skipped=skipped,
                rendered=rendered,
            )
            emitted_slugs.add("snapshot")

    for page_path, page in _included_pages(paths, include_slugs or []):
        if page.frontmatter.slug in emitted_slugs:
            continue
        _append_budgeted_fragment(
            output_format,
            query=normalized_query,
            budget=budget,
            slug=page.frontmatter.slug,
            title=page.frontmatter.title,
            relative_path=_relative(paths, page_path),
            page_type=getattr(page.frontmatter.type, "value", str(page.frontmatter.type)),
            compiled_truth=page.compiled_truth,
            timeline=page.timeline,
            sources=page.sources,
            fragments=fragments,
            skipped=skipped,
            rendered=rendered,
        )
        emitted_slugs.add(page.frontmatter.slug)

    for summary in retrieval.results:
        if summary.slug in emitted_slugs:
            continue
        if summary.slug in {"scratch-snapshot", "scratch-working"}:
            _append_budgeted_fragment(
                output_format,
                query=normalized_query,
                budget=budget,
                slug=summary.slug,
                title=summary.title,
                relative_path=summary.relative_path,
                page_type=str(getattr(summary.page_type, "value", summary.page_type)),
                compiled_truth=summary.compiled_truth,
                timeline=summary.recent_timeline,
                sources=[summary.relative_path],
                fragments=fragments,
                skipped=skipped,
                rendered=rendered,
            )
            emitted_slugs.add(summary.slug)
            continue
        page_path = paths.root / summary.relative_path
        if not page_path.exists() or not page_path.is_file():
            continue
        page = parse_page(page_path)
        _append_budgeted_fragment(
            output_format,
            query=normalized_query,
            budget=budget,
            slug=summary.slug,
            title=summary.title,
            relative_path=summary.relative_path,
            page_type=getattr(summary.page_type, "value", str(summary.page_type)),
            compiled_truth=page.compiled_truth,
            timeline=page.timeline,
            sources=page.sources,
            fragments=fragments,
            skipped=skipped,
            rendered=rendered,
        )
        emitted_slugs.add(summary.slug)

    content, used_tokens = _render_result(
        output_format,
        query=normalized_query,
        budget=budget,
        fragments=fragments,
        skipped=skipped,
        body=rendered,
    )
    warnings = list(retrieval.warnings)
    if any(fragment.truncated for fragment in fragments):
        warnings.append("One or more fragments were truncated to fit the token budget.")
    if skipped:
        warnings.append("One or more fragments were skipped because the token budget was exhausted.")

    return InjectionResult(
        query=normalized_query,
        budget=budget,
        used_tokens=used_tokens,
        fragment_count=len(fragments),
        output_format=output_format,
        mode=retrieval.mode,
        effective_mode=retrieval.effective_mode,
        top=top,
        content=content,
        fragments=fragments,
        skipped=skipped,
        warnings=warnings,
    )


def estimate_tokens(text: str) -> int:
    """Deterministic lightweight token estimate used for budget decisions."""
    if not text:
        return 0
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return max(1, len(encoding.encode(text)))
    except Exception:
        cjk_chars = len(CJK_RE.findall(text))
        other_chars = len(text) - cjk_chars
        return max(1, cjk_chars + math.ceil(other_chars / 3.5))


def _resolve_snapshot_path(paths: BrainPaths, snapshot_path: Path | None) -> Path:
    if snapshot_path is not None:
        return Path(snapshot_path).expanduser()
    return getattr(paths, "snapshot_path", paths.root / "scratch" / "SNAPSHOT.md")


def _read_snapshot(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None


def _included_pages(paths: BrainPaths, slugs: Sequence[str]) -> list[tuple[Path, object]]:
    wanted = [slug.strip() for slug in slugs if slug.strip()]
    if not wanted:
        return []
    by_slug: dict[str, tuple[Path, object]] = {}
    if paths.pages_dir.exists():
        for page_path in sorted(paths.pages_dir.rglob("*.md")):
            if page_path.name in {"index.md", "log.md"}:
                continue
            try:
                page = parse_page(page_path)
            except BrainError:
                continue
            by_slug[page.frontmatter.slug] = (page_path, page)
    missing = [slug for slug in wanted if slug not in by_slug]
    if missing:
        raise BrainError(f"include slug not found: {', '.join(missing)}")
    return [by_slug[slug] for slug in wanted]


def _relative(paths: BrainPaths, path: Path) -> str:
    try:
        return path.relative_to(paths.root).as_posix()
    except ValueError:
        return path.as_posix()


def _append_budgeted_fragment(
    output_format: OutputFormat,
    *,
    query: str,
    budget: int,
    slug: str,
    title: str,
    relative_path: str,
    page_type: str,
    compiled_truth: str,
    timeline: list[str],
    sources: list[str],
    fragments: list[InjectionFragment],
    skipped: list[SkippedFragment],
    rendered: list[str],
) -> None:
    full_fragment = _render_fragment(
        output_format,
        slug=slug,
        title=title,
        relative_path=relative_path,
        page_type=page_type,
        compiled_truth=compiled_truth,
        timeline=timeline,
        sources=sources,
        truncated=False,
    )
    full_tokens = estimate_tokens(full_fragment)

    full_fragment_model = InjectionFragment(
        slug=slug,
        title=title,
        relative_path=relative_path,
        token_count=full_tokens,
    )
    if _fits_verbose_budget(
        output_format,
        query=query,
        budget=budget,
        fragments=[*fragments, full_fragment_model],
        skipped=skipped,
        body=[*rendered, full_fragment],
    ):
        rendered.append(full_fragment)
        fragments.append(full_fragment_model)
        return

    if budget >= MIN_TRUNCATED_FRAGMENT_TOKENS:
        truncated_text = _truncate_to_final_budget(
            compiled_truth,
            budget,
            output_format=output_format,
            query=query,
            slug=slug,
            title=title,
            relative_path=relative_path,
            page_type=page_type,
            fragments=fragments,
            skipped=skipped,
            body=rendered,
        )
        if truncated_text is not None:
            truncated_fragment = _render_fragment(
                output_format,
                slug=slug,
                title=title,
                relative_path=relative_path,
                page_type=page_type,
                compiled_truth=truncated_text,
                timeline=[],
                sources=[],
                truncated=True,
            )
            truncated_tokens = estimate_tokens(truncated_fragment)
            rendered.append(truncated_fragment)
            fragments.append(
                InjectionFragment(
                    slug=slug,
                    title=title,
                    relative_path=relative_path,
                    token_count=truncated_tokens,
                    truncated=True,
                )
            )
            return

    skipped.append(
        SkippedFragment(
            slug=slug,
            relative_path=relative_path,
            reason="token budget exhausted",
        )
    )


def _render_result(
    output_format: OutputFormat,
    *,
    query: str,
    budget: int,
    fragments: list[InjectionFragment],
    skipped: list[SkippedFragment],
    body: list[str],
    allow_body_drop: bool = True,
) -> tuple[str, int]:
    builders = [
        lambda used: _render_verbose(
            output_format,
            query=query,
            budget=budget,
            used_tokens=used,
            fragments=fragments,
            skipped=skipped,
            body=body,
        ),
        lambda used: _render_compact(
            output_format,
            budget=budget,
            used_tokens=used,
            fragments=fragments,
            skipped=skipped,
            body=body,
        ),
        lambda used: _render_minimal(output_format, budget=budget, used_tokens=used),
    ]
    if allow_body_drop:
        builders.insert(
            -1,
            lambda used: _render_compact(
                output_format,
                budget=budget,
                used_tokens=used,
                fragments=fragments,
                skipped=skipped,
                body=[],
            ),
        )
    for builder in builders:
        content, used_tokens = _with_accurate_budget(builder)
        if used_tokens <= budget:
            return content, used_tokens

    return ".\n", estimate_tokens(".\n")


def _render_verbose(
    output_format: OutputFormat,
    *,
    query: str,
    budget: int,
    used_tokens: int,
    fragments: list[InjectionFragment],
    skipped: list[SkippedFragment],
    body: list[str],
) -> str:
    if output_format == "markdown":
        lines = [
            "# BrainMem Injection",
            "",
            f"- Query: {query}",
            f"- Budget used: {used_tokens}/{budget} estimated tokens",
            f"- Fragment count: {len(fragments)}",
        ]
        if fragments:
            lines.append(
                "- Sources: "
                + ", ".join(f"{fragment.slug} ({fragment.relative_path})" for fragment in fragments)
            )
        if any(fragment.truncated for fragment in fragments):
            lines.append("- Truncation: one or more fragments were shortened to fit the budget")
        if skipped:
            lines.append(
                "- Skipped: "
                + ", ".join(f"{fragment.slug} ({fragment.relative_path})" for fragment in skipped)
            )
        if body:
            lines.extend(["", "## Fragments", "", "\n\n".join(body)])
        return "\n".join(lines).rstrip() + "\n"

    lines = [
        "BrainMem Injection",
        f"Query: {query}",
        f"Budget used: {used_tokens}/{budget} estimated tokens",
        f"Fragment count: {len(fragments)}",
    ]
    if fragments:
        lines.append(
            "Sources: "
            + ", ".join(f"{fragment.slug} ({fragment.relative_path})" for fragment in fragments)
        )
    if any(fragment.truncated for fragment in fragments):
        lines.append("Truncation: one or more fragments were shortened to fit the budget")
    if skipped:
        lines.append(
            "Skipped: " + ", ".join(f"{fragment.slug} ({fragment.relative_path})" for fragment in skipped)
        )
    if body:
        lines.extend(["", "\n\n".join(body)])
    return "\n".join(lines).rstrip() + "\n"


def _render_compact(
    output_format: OutputFormat,
    *,
    budget: int,
    used_tokens: int,
    fragments: list[InjectionFragment],
    skipped: list[SkippedFragment],
    body: list[str],
) -> str:
    fragment_slugs = ", ".join(fragment.slug for fragment in fragments) or "none"
    skipped_slugs = ", ".join(fragment.slug for fragment in skipped)
    if output_format == "markdown":
        lines = [
            "# BrainMem Injection",
            f"- Budget used: {used_tokens}/{budget} estimated tokens",
            f"- Fragments: {len(fragments)} ({fragment_slugs})",
        ]
        if skipped:
            lines.append(f"- Skipped: {skipped_slugs}")
        if any(fragment.truncated for fragment in fragments):
            lines.append("- Truncated: yes")
        if body:
            lines.extend(["", *[_compact_fragment(text) for text in body]])
        return "\n".join(lines).rstrip() + "\n"

    lines = [
        "BrainMem Injection",
        f"Budget used: {used_tokens}/{budget} estimated tokens",
        f"Fragments: {len(fragments)} ({fragment_slugs})",
    ]
    if skipped:
        lines.append(f"Skipped: {skipped_slugs}")
    if any(fragment.truncated for fragment in fragments):
        lines.append("Truncated: yes")
    if body:
        lines.extend(["", *[_compact_fragment(text) for text in body]])
    return "\n".join(lines).rstrip() + "\n"


def _render_minimal(
    output_format: OutputFormat,
    *,
    budget: int,
    used_tokens: int,
) -> str:
    if output_format == "markdown":
        return f"# BrainMem Injection\n- Budget used: {used_tokens}/{budget} estimated tokens\n"
    return f"BrainMem Injection\nBudget used: {used_tokens}/{budget} estimated tokens\n"


def _with_accurate_budget(builder) -> tuple[str, int]:
    used_tokens = 0
    content = builder(used_tokens)
    for _ in range(8):
        next_used_tokens = estimate_tokens(content)
        if next_used_tokens == used_tokens:
            return content, next_used_tokens
        used_tokens = next_used_tokens
        content = builder(used_tokens)
    return content, estimate_tokens(content)


def _compact_fragment(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    source = next((line for line in lines if "Source:" in line), "")
    marker = "truncated to fit token budget" if "truncated to fit token budget" in text else ""
    return " ".join(line for line in [source, marker] if line)


def _render_fragment(
    output_format: OutputFormat,
    *,
    slug: str,
    title: str,
    relative_path: str,
    page_type: str,
    compiled_truth: str,
    timeline: list[str],
    sources: list[str],
    truncated: bool,
) -> str:
    truncation = " (truncated)" if truncated else ""
    if output_format == "markdown":
        lines = [
            f"### {title} (`{slug}`){truncation}",
            "",
            f"- Source: {relative_path}",
            f"- Type: {page_type}",
            "",
            "Compiled truth:",
            compiled_truth.strip(),
        ]
        if timeline:
            lines.extend(["", "Recent timeline:", *timeline[:3]])
        if sources:
            lines.extend(["", "Sources:", *[f"- {source}" for source in sources]])
        return "\n".join(lines).rstrip()

    lines = [
        f"{title} [{slug}]{truncation}",
        f"Source: {relative_path}",
        f"Type: {page_type}",
        "Compiled truth:",
        compiled_truth.strip(),
    ]
    if timeline:
        lines.extend(["Recent timeline:", *timeline[:3]])
    if sources:
        lines.extend(["Sources:", *sources])
    return "\n".join(lines).rstrip()


def _truncate_to_final_budget(
    text: str,
    budget: int,
    *,
    output_format: OutputFormat,
    query: str,
    slug: str,
    title: str,
    relative_path: str,
    page_type: str,
    fragments: list[InjectionFragment],
    skipped: list[SkippedFragment],
    body: list[str],
) -> str | None:
    suffix = "\n[truncated to fit token budget]"
    low = 0
    high = len(text)
    best: str | None = None
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + suffix
        rendered = _render_fragment(
            output_format,
            slug=slug,
            title=title,
            relative_path=relative_path,
            page_type=page_type,
            compiled_truth=candidate,
            timeline=[],
            sources=[],
            truncated=True,
        )
        fragment = InjectionFragment(
            slug=slug,
            title=title,
            relative_path=relative_path,
            token_count=estimate_tokens(rendered),
            truncated=True,
        )
        if _fits_budget(
            output_format,
            query=query,
            budget=budget,
            fragments=[*fragments, fragment],
            skipped=skipped,
            body=[*body, rendered],
        ):
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _fits_budget(
    output_format: OutputFormat,
    *,
    query: str,
    budget: int,
    fragments: list[InjectionFragment],
    skipped: list[SkippedFragment],
    body: list[str],
) -> bool:
    _, verbose_tokens = _with_accurate_budget(
        lambda used: _render_verbose(
            output_format,
            query=query,
            budget=budget,
            used_tokens=used,
            fragments=fragments,
            skipped=skipped,
            body=body,
        )
    )
    if verbose_tokens <= budget:
        return True
    _, compact_tokens = _with_accurate_budget(
        lambda used: _render_compact(
            output_format,
            budget=budget,
            used_tokens=used,
            fragments=fragments,
            skipped=skipped,
            body=body,
        )
    )
    return compact_tokens <= budget


def _fits_verbose_budget(
    output_format: OutputFormat,
    *,
    query: str,
    budget: int,
    fragments: list[InjectionFragment],
    skipped: list[SkippedFragment],
    body: list[str],
) -> bool:
    _, used_tokens = _with_accurate_budget(
        lambda used: _render_verbose(
            output_format,
            query=query,
            budget=budget,
            used_tokens=used,
            fragments=fragments,
            skipped=skipped,
            body=body,
        )
    )
    return used_tokens <= budget
