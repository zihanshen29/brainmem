"""Evidence summaries with human-edit protection and reviewable refresh drafts."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from contextlib import closing
from pathlib import Path

from brain.concurrency import root_lock
from brain.db.connection import connect
from brain.exceptions import BrainError
from brain.pages import parse_page, write_page
from brain.pages.timeline import parse_entry
from brain.predicates import fact_sentence
from brain.privacy import require_external, split_provenance

STUB = "(stub - waiting for more evidence)"


def summary_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_summary(conn, page) -> str:
    rows = conn.execute(
        "SELECT predicate, object FROM facts WHERE subject = ? AND superseded_by IS NULL "
        "AND valid_to IS NULL ORDER BY asserted_at DESC, id DESC LIMIT 8",
        (page.frontmatter.slug,),
    ).fetchall()
    chinese = bool(re.search(r"[\u4e00-\u9fff]", page.frontmatter.title + " ".join(page.timeline)))
    if rows:
        return "\n".join(
            dict.fromkeys(
                fact_sentence(page.frontmatter.title, row[0], row[1], chinese=chinese)
                for row in rows
            )
        )
    descriptions = []
    for line in reversed(page.timeline):
        body, _ = split_provenance(parse_entry(line).description)
        if body and body not in descriptions:
            descriptions.append(body)
        if len(descriptions) == 5:
            break
    return "\n".join(descriptions)


def refresh_generated_summary(conn, path: Path, entity_id: str) -> None:
    page = parse_page(path)
    if page.frontmatter.curated:
        return
    if page.compiled_truth != STUB and page.frontmatter.summary_hash != summary_hash(
        page.compiled_truth
    ):
        return
    text = evidence_summary(conn, page)
    if not text:
        return
    page.compiled_truth = text
    page.frontmatter.summary_hash = summary_hash(text)
    write_page(path, page)


def propose_summary(root: Path, slug: str, *, provider: bool = False) -> dict:
    from brain.config_context import configured_path
    from brain.paths import BrainPaths
    from brain.pipeline.ingest import IngestReport, ReviewWriter
    from brain.pipeline.rebuild import _resolve_unique_page

    with root_lock(root), closing(connect(root / "brain.db", read_only=True)) as conn:
        path, page = _resolve_unique_page(BrainPaths(root), slug)
        original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        text = evidence_summary(conn, page)
        if provider:
            require_external(root, path=path)
    if provider:
        from brain.llm.client import rewrite_compiled_truth

        with configured_path(root / "config.toml"):
            text = rewrite_compiled_truth(
                [parse_entry(line) for line in page.timeline], page.compiled_truth
            )
    if not text.strip():
        raise BrainError("No accepted evidence available for a summary")
    diff = "\n".join(
        difflib.unified_diff(
            page.compiled_truth.splitlines(),
            text.splitlines(),
            fromfile="current",
            tofile="proposed",
            lineterm="",
        )
    )
    payload = {
        "page_path": path.relative_to(root).as_posix(),
        "page_hash": original_hash,
        "compiled_truth": text,
        "provider": provider,
    }
    with root_lock(root, write=True):
        if hashlib.sha256(path.read_bytes()).hexdigest() != original_hash:
            raise BrainError("Page changed while preparing summary; generate a fresh draft")
        if provider:
            require_external(root, path=path)
        writer = ReviewWriter.create(BrainPaths(root), IngestReport())
        review = writer.write(
            "summary_refresh",
            "# Summary draft\n\n```json\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n```\n\n```diff\n"
            + diff
            + "\n```",
        )
    return {"review_file": review, "page": payload["page_path"], "diff": diff}
