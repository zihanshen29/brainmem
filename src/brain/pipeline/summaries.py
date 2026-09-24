"""Evidence summaries with human-edit protection and reviewable refresh drafts."""
# Chinese punctuation below is intentional user-facing prose.
# ruff: noqa: RUF001

from __future__ import annotations

import difflib
import hashlib
import json
from contextlib import closing
from pathlib import Path

from brain.concurrency import root_lock
from brain.db.connection import connect
from brain.exceptions import BrainError
from brain.pages import parse_page, write_page
from brain.pages.timeline import TimelineEntry, parse_entry
from brain.predicates import VOCABULARY, normalize_predicate, uses_chinese
from brain.privacy import require_external, split_provenance

STUB = "(stub - waiting for more evidence)"

# Rendering vocabulary is separate from relationship cardinality. Unknown relations
# stay in the facts store; we explicitly label incomplete local drafts.
SUMMARY_LABELS = {
    "goal": "目标", "purpose": "用途", "objective": "目标", "direction": "方向",
    "strategy": "方案", "runs_on": "运行于", "backend_framework": "后端",
    "frontend_framework": "前端", "online_url": "访问地址", "released_on": "发布时间",
    "completed": "已完成", "built_and_verified": "构建验证", "verified": "验证",
    "trained_with": "训练方式", "has_script": "相关脚本", "deployment_name": "部署名称",
    "current_commit": "当前提交", "source_commit": "源代码提交", "has_git_commit": "代码提交",
    "branch": "分支", "release_id": "发布版本", "rule_version": "规则版本",
    "local_backend_tests_passed": "后端测试通过", "local_browser_tests_passed": "浏览器测试通过",
    "public_readonly_browser_acceptance": "公开只读验收", "real_deepseek_scenarios_passed": "模型场景验证",
    "passed_survivability_episodes": "通过的生存测试轮数", "passed_survivability_falls": "生存测试跌倒次数",
}
GROUPS = (("方向与决策", "Direction and decisions"), ("进展", "Progress"),
          ("实现与运行", "Implementation"), ("验证", "Validation"), ("版本记录", "Versions"))


def _active_evidence(conn, slug):
    return conn.execute(
        "SELECT * FROM facts WHERE subject = ? AND superseded_by IS NULL "
        "AND valid_to IS NULL ORDER BY asserted_at DESC, id DESC", (slug,)
    ).fetchall()


def _group(predicate: str) -> int:
    if predicate in {"prefers", "decided", "goal", "purpose", "objective", "direction", "strategy", "role", "works_as"}:
        return 0
    if predicate in {"status", "lifecycle_state", "completed", "released_on"}:
        return 1
    if predicate in {"current_commit", "source_commit", "has_git_commit", "committed", "branch", "release_id", "database_head", "rule_version"}:
        return 4
    if "passed" in predicate or "verified" in predicate or "acceptance" in predicate:
        return 3
    return 2


def summary_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cjk_join(prefix: str, value: str) -> str:
    # Chinese typesetting keeps a space between Han text and Latin words or digits.
    if prefix and value and "一" <= prefix[-1] <= "鿿" and value[0].isascii() and value[0].isalnum():
        return f"{prefix} {value}"
    return f"{prefix}{value}"


def _phrase(predicate: str, label: str, value: str, chinese: bool) -> str:
    if not chinese:
        return value if predicate == "status" else f"{predicate.replace('_', ' ')}: {value}"
    if predicate == "status":
        return value
    if predicate == "decided":
        return _cjk_join("已决定", value)
    if predicate in {"uses", "prefers", "runs_on", "works_at", "related_to", "passed", "completed"}:
        return _cjk_join(label, value)
    if predicate in {"backend_framework", "frontend_framework"}:
        return _cjk_join(f"{label}使用", value)
    if predicate == "released_on":
        return f"于 {value} 发布"
    return f"{label}为 {value}"


def evidence_summary(conn, page, output_language: str = "source") -> str:
    rows = _active_evidence(conn, page.frontmatter.slug)
    chinese = uses_chinese(
        page.frontmatter.title + " ".join(page.timeline) + " ".join(row["object"] for row in rows),
        output_language,
    )
    if rows:
        groups: dict[int, list[str]] = {}
        omitted = 0  # unsupported relations plus facts beyond each group's display limit
        for row in rows:
            predicate = normalize_predicate(row["predicate"])
            entry = VOCABULARY.get(predicate)
            label = SUMMARY_LABELS.get(predicate) or (entry[2] if entry else None)
            if label is None:
                omitted += 1
                continue
            value = row["object"]
            if row["object_type"] == "entity":
                entity = conn.execute("SELECT title FROM entities WHERE id = ?", (value,)).fetchone()
                if entity:
                    value = entity[0]
            phrase = _phrase(predicate, label, value, chinese)
            group = groups.setdefault(_group(predicate), [])
            phrase = phrase.rstrip("。.")
            if phrase in group:
                continue
            if len(group) < 3:
                group.append(phrase)
            else:
                omitted += 1
        paragraphs = [
            f"{GROUPS[key][0 if chinese else 1]}：" + "；".join(values) + "。" if chinese
            else f"{GROUPS[key][1]}: " + "; ".join(values) + "."
            for key, values in sorted(groups.items())
        ]
        if not paragraphs:
            return (f"本地摘要占位：已有 {len(rows)} 条事实，但这些关系尚未支持自然语言整理。请查看时间线，或生成模型摘要草稿。"
                    if chinese else f"Local summary placeholder: {len(rows)} accepted facts use unsupported relations. Consult the timeline or request a provider draft.")
        if omitted:
            paragraphs.append(f"另有 {omitted} 条事实未列入本地摘要；可查看时间线或生成模型摘要草稿补充。" if chinese
                              else f"{omitted} more facts are not listed in this local summary; see the timeline or request a provider draft.")
        return "\n\n".join(paragraphs)
    descriptions = []
    for line in reversed(page.timeline):
        body, _ = split_provenance(parse_entry(line).description)
        if body and body not in descriptions:
            descriptions.append(body)
        if len(descriptions) == 5:
            break
    return "\n".join(descriptions)


def refresh_generated_summary(
    conn, path: Path, entity_id: str, *, output_language: str = "source"
) -> None:
    page = parse_page(path)
    if page.frontmatter.curated:
        return
    if page.compiled_truth != STUB and page.frontmatter.summary_hash != summary_hash(
        page.compiled_truth
    ):
        return
    text = evidence_summary(conn, page, output_language)
    if not text:
        return
    page.compiled_truth = text
    page.frontmatter.summary_hash = summary_hash(text)
    write_page(path, page)


def propose_summary(root: Path, slug: str, *, provider: bool = False, dry_run: bool = False) -> dict:
    from brain.config import load_config
    from brain.config_context import configured_path
    from brain.paths import BrainPaths
    from brain.pipeline.ingest import IngestReport, ReviewWriter
    from brain.pipeline.rebuild import _resolve_unique_page

    if dry_run and provider:
        raise BrainError("Summary dry-run is local only; omit --provider")
    with root_lock(root), closing(connect(root / "brain.db", read_only=True)) as conn:
        path, page = _resolve_unique_page(BrainPaths(root), slug)
        original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        config_path = root / "config.toml"
        output_language = (
            load_config(config_path).ingest.output_language if config_path.exists() else "source"
        )
        text = evidence_summary(conn, page, output_language)
        facts = _active_evidence(conn, slug)
        if provider:
            require_external(root, path=path)
            for row in facts:
                require_external(root, text=row["object"], path=row["source_ref"] if not (row["source_ref"] or "").startswith(("https://", "http://")) else None)
    if provider:
        from brain.llm.client import rewrite_compiled_truth

        with configured_path(root / "config.toml"):
            text = rewrite_compiled_truth(
                [parse_entry(line) for line in page.timeline] + [
                    TimelineEntry(date=row["valid_from"] or row["asserted_at"][:10],
                                  event_id=row["source_event"],
                                  description=json.dumps({"accepted_fact": dict(row)}, ensure_ascii=False))
                    for row in facts
                ], page.compiled_truth
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
    if dry_run:
        return {"dry_run": True, "page": payload["page_path"], "compiled_truth": text, "diff": diff}
    with root_lock(root, write=True):
        if hashlib.sha256(path.read_bytes()).hexdigest() != original_hash:
            raise BrainError("Page changed while preparing summary; generate a fresh draft")
        if provider:
            require_external(root, path=path)
            for row in facts:
                require_external(root, text=row["object"], path=row["source_ref"] if not (row["source_ref"] or "").startswith(("https://", "http://")) else None)
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
