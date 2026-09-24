"""Turn mistaken path/file/label entities into literal values through a reviewed plan.

Object references become the entity title as a literal value. With a fold
target, facts whose subject is such an entity move to the target project and
keep the entity's name in the predicate, and a generated stub page hands its
timeline and sources to the target page before it is removed.
"""

from __future__ import annotations

import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import ulid

from brain.backup import file_hash, require_current_backup
from brain.concurrency import root_lock
from brain.config import load_config
from brain.db.connection import connect
from brain.db.embeddings import delete_page_embeddings
from brain.exceptions import BrainError
from brain.ledger import append_event
from brain.models import Event, EventKind
from brain.pages import append_log, parse_page, regenerate_index, write_page
from brain.pipeline.reconcile import root_fingerprint
from brain.pipeline.summaries import STUB
from brain.transactions import atomic_text, durable_unit, protect_path

_PREDICATE = re.compile(r"[a-z][a-z0-9_]*")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def plan_literalize(
    root: Path,
    entity_ids: list[str],
    predicates: dict[int, str] | None = None,
    *,
    fold_into: str | None = None,
    dangling: bool = False,
) -> dict:
    """Read-only plan. Apply recomputes it and refuses any difference."""
    request = _request(entity_ids, predicates or {}, fold_into, dangling)
    with root_lock(root), closing(connect(root / "brain.db", read_only=True)) as conn:
        return _plan(root, conn, request)


def apply_literalize(root: Path, plan: dict, backup: Path) -> dict:
    from brain.git_ops import check_commit_paths, commit
    from brain.pipeline.entity_merge import _stageable_commit_paths

    with root_lock(root, write=True):
        require_current_backup(root, backup)
        request = plan.get("request") or {}
        with closing(connect(root / "brain.db", read_only=True)) as conn:
            current = _plan(
                root,
                conn,
                _request(
                    request.get("entities", []),
                    request.get("predicates", {}),
                    request.get("fold_into"),
                    bool(request.get("dangling")),
                ),
            )
        if plan != current:
            raise BrainError("Plan is stale or was modified; create and review a fresh dry-run")
        if plan["errors"]:
            raise BrainError("Resolve the plan errors before applying")
        if not plan["entities"] and not plan["facts"]:
            return {"applied": True, "entities_removed": 0, "facts_updated": 0, "commit": None}

        config = load_config(root / "config.toml")
        rewritten = plan["pages_rewritten"]
        commit_paths = [
            root / "brain.db",
            root / "events.jsonl",
            root / "pages" / "log.md",
            root / "pages" / "index.md",
            *[root / page["path"] for page in plan["pages_removed"]],
            *[root / relative for relative in rewritten],
        ]
        if plan["target"]:
            commit_paths.append(root / plan["target"]["path"])
        commit_paths = list(dict.fromkeys(commit_paths))
        if config.git.auto_commit:
            check_commit_paths(root, [path for path in commit_paths if path.exists()])
        before = {path: file_hash(path) if path.is_file() else None for path in commit_paths}
        with closing(connect(root / "brain.db")) as conn:
            with durable_unit(root, conn, "literalize:" + plan["fingerprint"]):
                _apply_changes(root, conn, plan, rewritten, config.ingest.output_language)
            # Git commits the main database file, so fold the WAL into it first.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sha = None
        if config.git.auto_commit:
            changed = [
                path for path in commit_paths
                if (file_hash(path) if path.is_file() else None) != before[path]
            ]
            sha = commit(
                root,
                f"entity literalize: {len(plan['entities'])} entities, {len(plan['facts'])} facts",
                paths=_stageable_commit_paths(root, changed),
            )
    return {
        "applied": True,
        "entities_removed": len(plan["entities"]),
        "facts_updated": len(plan["facts"]),
        "predicate_corrections": plan["counts"]["predicate_corrections"],
        "pages_removed": len(plan["pages_removed"]),
        "commit": sha,
    }


def _apply_changes(root: Path, conn, plan: dict, rewritten: list[str], language: str) -> None:
    from brain.paths import BrainPaths
    from brain.pipeline.entity_merge import _merge_timeline
    from brain.pipeline.ingest import _rebuild_touched_backlinks
    from brain.pipeline.summaries import refresh_generated_summary

    for change in plan["facts"]:
        before, after = change["before"], change["after"]
        cursor = conn.execute(
            "UPDATE facts SET subject = ?, predicate = ?, object = ?, object_type = ? "
            "WHERE id = ? AND subject = ? AND predicate = ? AND object = ? AND object_type = ?",
            (after["subject"], after["predicate"], after["object"], after["object_type"], change["id"],
             before["subject"], before["predicate"], before["object"], before["object_type"]),
        )
        if cursor.rowcount != 1:
            raise BrainError(f"Fact {change['id']} changed while applying the plan")
    ids = [entity["id"] for entity in plan["entities"]]
    for entity_id in ids:
        conn.execute(
            "DELETE FROM backlinks WHERE to_entity = ? OR from_page = ?", (entity_id, entity_id)
        )
        conn.execute("DELETE FROM tier_proposals WHERE entity_id = ?", (entity_id,))
        delete_page_embeddings(conn, entity_id)
        conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (entity_id,))
        conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))

    touched = list(rewritten)
    target = plan["target"]
    if target:
        path = root / target["path"]
        if plan["pages_removed"]:
            page = parse_page(path)
            moved = [line for removed in plan["pages_removed"] for line in removed["timeline"]]
            page.timeline = _merge_timeline(page.timeline, moved)
            page.sources = list(dict.fromkeys([*page.sources, *target["sources_added"]]))
            page.frontmatter.updated = datetime.now(UTC)
            write_page(path, page)
        # Moved facts may change a machine-owned summary; edited text is left alone.
        refresh_generated_summary(conn, path, target["slug"], output_language=language)
        touched.append(target["path"])
    for removed in plan["pages_removed"]:
        path = root / removed["path"]
        protect_path(path, None)
        path.unlink()
    titles = {entity["id"]: entity["title"] for entity in plan["entities"]}
    for relative in rewritten:
        path = root / relative
        text = path.read_text(encoding="utf-8")
        atomic_text(path, _WIKILINK.sub(lambda match: _plain_link(match, titles), text))
    _rebuild_touched_backlinks(conn, BrainPaths(root), list(dict.fromkeys(touched)))
    if plan["pages_removed"]:
        regenerate_index(root)

    now = datetime.now(UTC)
    append_event(
        root / "events.jsonl",
        Event(
            id=str(ulid.ULID()),
            timestamp=now,
            kind=EventKind.PAGE_EDITED,
            source_ref="entity_literalize:" + (",".join(ids) or "dangling"),
            affected_pages=[*[page["path"] for page in plan["pages_removed"]],
                            *([target["path"]] if target else [])],
            metadata={
                "action": "entity_literalize",
                "entities": ids,
                "fold_into": target["slug"] if target else None,
                "facts": [
                    {"id": change["id"], "before": change["before"], "after": change["after"]}
                    for change in plan["facts"]
                ],
            },
        ),
    )
    append_log(
        root,
        f"- {now.strftime('%Y-%m-%d %H:%M')} entity literalize: {len(ids)} entities, "
        f"{len(plan['facts'])} facts" + (f", folded into {target['slug']}" if target else ""),
    )


def _plain_link(match: re.Match[str], titles: dict[str, str]) -> str:
    target = match.group(1).strip()
    if target not in titles:
        return match.group(0)
    return match.group(2) or titles[target]


def _pages_linking(root: Path, ids: list[str]) -> list[str]:
    if not ids:
        return []
    wanted = set(ids)
    pages = []
    for path in sorted((root / "pages").rglob("*.md")):
        if path.stem in wanted:
            continue
        text = path.read_text(encoding="utf-8")
        if any(match.group(1).strip() in wanted for match in _WIKILINK.finditer(text)):
            pages.append(path.relative_to(root).as_posix())
    return pages


def _request(entity_ids: list[str], predicates: dict, fold_into: str | None, dangling: bool) -> dict:
    ids = list(dict.fromkeys(value.strip() for value in entity_ids if value.strip()))
    if not ids and not dangling:
        raise BrainError("At least one entity id is required")
    corrections: dict[str, str] = {}
    for fact_id, predicate in predicates.items():
        name = str(predicate).strip()
        if not _PREDICATE.fullmatch(name):
            raise BrainError(f"Corrected predicate must be snake_case: {predicate!r}")
        corrections[str(int(fact_id))] = name
    # JSON keeps string keys; sorting makes the reviewed file and the recomputed plan equal.
    return {
        "entities": ids,
        "predicates": dict(sorted(corrections.items(), key=lambda item: int(item[0]))),
        "fold_into": fold_into.strip() if fold_into and fold_into.strip() else None,
        "dangling": bool(dangling),
    }


def _plan(root: Path, conn, request: dict) -> dict:
    plan: dict = {
        "version": 2,
        "request": request,
        "fingerprint": root_fingerprint(root),
        "target": None,
        "entities": [],
        "absent": [],
        "facts": [],
        "pages_removed": [],
        "errors": [],
    }
    fold = request["fold_into"]
    if fold:
        row = conn.execute("SELECT id, page_path FROM entities WHERE id = ?", (fold,)).fetchone()
        if row is None or not row["page_path"] or not (root / row["page_path"]).is_file():
            plan["errors"].append({"entity": fold, "error": "fold target needs an existing page"})
            fold = None
        elif fold in request["entities"]:
            plan["errors"].append({"entity": fold, "error": "cannot fold an entity into itself"})
            fold = None
        else:
            plan["target"] = {"slug": fold, "path": row["page_path"], "sources_added": []}

    titles: dict[str, str] = {}
    for entity_id in request["entities"]:
        row = conn.execute(
            "SELECT id, type, title, page_path FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            # Already literal, merged away or never registered; nothing left to change.
            plan["absent"].append(entity_id)
            continue
        if not row["title"].strip():
            plan["errors"].append({"entity": entity_id, "error": "empty title cannot become a value"})
        subjects = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE subject = ?", (entity_id,)
        ).fetchone()[0]
        if subjects and not fold:
            plan["errors"].append(
                {"entity": entity_id, "error": f"subject of {subjects} facts; pass --fold-into a project"}
            )
        page_path = row["page_path"] if row["page_path"] and (root / row["page_path"]).is_file() else None
        if page_path:
            page = parse_page(root / page_path)
            if not fold:
                plan["errors"].append(
                    {"entity": entity_id, "error": "has a page; pass --fold-into or merge it instead"}
                )
            elif page.compiled_truth.strip() != STUB or page.frontmatter.curated:
                plan["errors"].append(
                    {"entity": entity_id, "error": "page holds its own text; merge it instead"}
                )
            else:
                plan["pages_removed"].append(
                    {"path": page_path, "timeline": list(page.timeline), "sources": list(page.sources)}
                )
        titles[entity_id] = row["title"]
        plan["entities"].append(
            {
                "id": entity_id,
                "type": row["type"],
                "title": row["title"],
                "page_path": page_path,
                "aliases": [
                    alias
                    for (alias,) in conn.execute(
                        "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias",
                        (entity_id,),
                    )
                ],
                "backlinks": [
                    [from_page, to_entity, relation]
                    for from_page, to_entity, relation in conn.execute(
                        "SELECT from_page, to_entity, relation FROM backlinks "
                        "WHERE to_entity = ? OR from_page = ? ORDER BY from_page, to_entity, relation",
                        (entity_id, entity_id),
                    )
                ],
                "tier_proposals": conn.execute(
                    "SELECT COUNT(*) FROM tier_proposals WHERE entity_id = ?", (entity_id,)
                ).fetchone()[0],
                "embeddings": conn.execute(
                    "SELECT COUNT(*) FROM embedding_index WHERE page_slug = ?", (entity_id,)
                ).fetchone()[0],
            }
        )
    if plan["target"]:
        target_page = parse_page(root / plan["target"]["path"])
        plan["target"]["sources_added"] = list(dict.fromkeys(
            source for removed in plan["pages_removed"] for source in removed["sources"]
            if source not in target_page.sources
        ))
    plan["pages_rewritten"] = _pages_linking(root, [entity["id"] for entity in plan["entities"]])

    rows = []
    if titles:
        marks = ",".join("?" for _ in titles)
        rows = conn.execute(
            "SELECT * FROM facts WHERE (object_type = 'entity' AND object IN "
            f"({marks})) OR subject IN ({marks}) ORDER BY id",
            [*titles, *titles],
        ).fetchall()
    if request["dangling"]:
        seen = {row["id"] for row in rows}
        rows = sorted(
            [*rows, *[row for row in conn.execute(
                "SELECT * FROM facts f WHERE object_type = 'entity' AND NOT EXISTS "
                "(SELECT 1 FROM entities e WHERE e.id = f.object) ORDER BY id"
            ) if row["id"] not in seen]],
            key=lambda row: row["id"],
        )
    for row in rows:
        before = {"subject": row["subject"], "predicate": row["predicate"],
                  "object": row["object"], "object_type": row["object_type"]}
        after = dict(before)
        if row["subject"] in titles and fold:
            # The fact now speaks about the project, so the stage or file name moves into
            # the relation instead of being lost.
            after["subject"] = fold
            after["predicate"] = f"{titles[row['subject']]} {row['predicate']}"
        if row["object_type"] == "entity" and (row["object"] in titles or request["dangling"]):
            after["object"] = titles.get(row["object"], row["object"])
            after["object_type"] = "literal"
        after["predicate"] = request["predicates"].get(str(row["id"]), after["predicate"])
        if after == before:
            continue
        plan["facts"].append({
            "id": row["id"],
            "active": row["superseded_by"] is None and row["valid_to"] is None,
            "before": before,
            "after": after,
        })

    planned = {str(change["id"]) for change in plan["facts"]}
    plan["errors"].extend(
        {"fact": int(fact_id), "error": "predicate correction targets a fact outside this plan"}
        for fact_id in request["predicates"]
        if fact_id not in planned
    )
    plan["counts"] = {
        "entities": len(plan["entities"]),
        "absent": len(plan["absent"]),
        "facts": len(plan["facts"]),
        "moved_to_target": sum(
            change["before"]["subject"] != change["after"]["subject"] for change in plan["facts"]
        ),
        "predicate_corrections": sum(
            change["before"]["predicate"] != change["after"]["predicate"]
            and change["before"]["subject"] == change["after"]["subject"]
            for change in plan["facts"]
        ),
        "pages_removed": len(plan["pages_removed"]),
        "errors": len(plan["errors"]),
    }
    return plan
