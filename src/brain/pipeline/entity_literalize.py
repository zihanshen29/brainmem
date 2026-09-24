"""Turn mistaken path/file entities into literal fact values through a reviewed plan."""

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
from brain.pages import append_log
from brain.pipeline.reconcile import root_fingerprint
from brain.transactions import durable_unit

_PREDICATE = re.compile(r"[a-z][a-z0-9_]*")


def plan_literalize(
    root: Path, entity_ids: list[str], predicates: dict[int, str] | None = None
) -> dict:
    """Read-only plan. Apply recomputes it and refuses any difference."""
    request = _request(entity_ids, predicates or {})
    with root_lock(root), closing(connect(root / "brain.db", read_only=True)) as conn:
        return _plan(root, conn, request)


def apply_literalize(root: Path, plan: dict, backup: Path) -> dict:
    from brain.git_ops import check_commit_paths, commit

    with root_lock(root, write=True):
        require_current_backup(root, backup)
        request = plan.get("request") or {}
        with closing(connect(root / "brain.db", read_only=True)) as conn:
            current = _plan(
                root,
                conn,
                _request(request.get("entities", []), request.get("predicates", {})),
            )
        if plan != current:
            raise BrainError("Plan is stale or was modified; create and review a fresh dry-run")
        if plan["errors"]:
            raise BrainError("Resolve the plan errors before applying")
        if not plan["entities"]:
            return {"applied": True, "entities_removed": 0, "facts_updated": 0, "commit": None}

        config = load_config(root / "config.toml")
        commit_paths = [root / "brain.db", root / "events.jsonl", root / "pages" / "log.md"]
        if config.git.auto_commit:
            check_commit_paths(root, commit_paths)
        before = {path: file_hash(path) if path.is_file() else None for path in commit_paths}
        ids = [entity["id"] for entity in plan["entities"]]
        with closing(connect(root / "brain.db")) as conn:
            with durable_unit(root, conn, "literalize:" + plan["fingerprint"]):
                _apply_changes(root, conn, plan, ids)
            # Git commits the main database file, so fold the WAL into it first.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sha = None
        if config.git.auto_commit:
            changed = [path for path in commit_paths if path.is_file() and file_hash(path) != before[path]]
            sha = commit(
                root, f"entity literalize: {len(ids)} artifact entities -> literal values", paths=changed
            )
    return {
        "applied": True,
        "entities_removed": len(ids),
        "facts_updated": len(plan["facts"]),
        "predicate_corrections": plan["counts"]["predicate_corrections"],
        "commit": sha,
    }


def _apply_changes(root: Path, conn, plan: dict, ids: list[str]) -> None:
    for change in plan["facts"]:
        cursor = conn.execute(
            "UPDATE facts SET predicate = ?, object = ?, object_type = 'literal' "
            "WHERE id = ? AND object = ? AND object_type = 'entity'",
            (
                change["after"]["predicate"],
                change["after"]["object"],
                change["id"],
                change["before"]["object"],
            ),
        )
        if cursor.rowcount != 1:
            raise BrainError(f"Fact {change['id']} changed while applying the plan")
    for entity_id in ids:
        conn.execute(
            "DELETE FROM backlinks WHERE to_entity = ? OR from_page = ?", (entity_id, entity_id)
        )
        conn.execute("DELETE FROM tier_proposals WHERE entity_id = ?", (entity_id,))
        delete_page_embeddings(conn, entity_id)
        conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (entity_id,))
        conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
    now = datetime.now(UTC)
    append_event(
        root / "events.jsonl",
        Event(
            id=str(ulid.ULID()),
            timestamp=now,
            kind=EventKind.PAGE_EDITED,
            source_ref="entity_literalize:" + ",".join(ids),
            metadata={
                "action": "entity_literalize",
                "entities": ids,
                "facts": [
                    {"id": change["id"], "before": change["before"], "after": change["after"]}
                    for change in plan["facts"]
                ],
            },
        ),
    )
    append_log(
        root,
        f"- {now.strftime('%Y-%m-%d %H:%M')} entity literalize: "
        f"{len(ids)} entities, {len(plan['facts'])} facts",
    )


def _request(entity_ids: list[str], predicates: dict) -> dict:
    ids = list(dict.fromkeys(value.strip() for value in entity_ids if value.strip()))
    if not ids:
        raise BrainError("At least one entity id is required")
    corrections: dict[str, str] = {}
    for fact_id, predicate in predicates.items():
        name = str(predicate).strip()
        if not _PREDICATE.fullmatch(name):
            raise BrainError(f"Corrected predicate must be snake_case: {predicate!r}")
        corrections[str(int(fact_id))] = name
    # JSON keeps string keys; sorting makes the reviewed file and the recomputed plan equal.
    return {"entities": ids, "predicates": dict(sorted(corrections.items(), key=lambda item: int(item[0])))}


def _plan(root: Path, conn, request: dict) -> dict:
    plan: dict = {
        "version": 1,
        "request": request,
        "fingerprint": root_fingerprint(root),
        "entities": [],
        "absent": [],
        "facts": [],
        "errors": [],
    }
    titles: dict[str, str] = {}
    for entity_id in request["entities"]:
        row = conn.execute(
            "SELECT id, type, title, page_path FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            # Already literal, merged away or never registered; nothing left to change.
            plan["absent"].append(entity_id)
            continue
        subjects = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE subject = ?", (entity_id,)
        ).fetchone()[0]
        if subjects:
            plan["errors"].append(
                {"entity": entity_id, "error": f"subject of {subjects} facts; a literal value cannot be a subject"}
            )
        if row["page_path"] and (root / row["page_path"]).is_file():
            plan["errors"].append(
                {"entity": entity_id, "error": "has a page; merge it or prune the stub instead"}
            )
        if not row["title"].strip():
            plan["errors"].append({"entity": entity_id, "error": "empty title cannot become a value"})
        titles[entity_id] = row["title"]
        plan["entities"].append(
            {
                "id": entity_id,
                "type": row["type"],
                "title": row["title"],
                "page_path": row["page_path"],
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
    if titles:
        marks = ",".join("?" for _ in titles)
        rows = conn.execute(
            "SELECT id, subject, predicate, object, superseded_by, valid_to FROM facts "
            f"WHERE object_type = 'entity' AND object IN ({marks}) ORDER BY id",
            list(titles),
        ).fetchall()
    else:
        rows = []
    for row in rows:
        plan["facts"].append(
            {
                "id": row["id"],
                "subject": row["subject"],
                "active": row["superseded_by"] is None and row["valid_to"] is None,
                "before": {"predicate": row["predicate"], "object": row["object"], "object_type": "entity"},
                "after": {
                    "predicate": request["predicates"].get(str(row["id"]), row["predicate"]),
                    "object": titles[row["object"]],
                    "object_type": "literal",
                },
            }
        )
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
        "predicate_corrections": sum(
            change["before"]["predicate"] != change["after"]["predicate"] for change in plan["facts"]
        ),
        "errors": len(plan["errors"]),
    }
    return plan
