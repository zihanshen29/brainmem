"""Read-only migration planning; guarded, non-destructive application."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from contextlib import closing
from pathlib import Path

from brain.backup import file_hash, require_current_backup
from brain.concurrency import root_lock
from brain.db.connection import connect
from brain.db.entities import add_alias, get_entity, lookup_by_alias, upsert_entity
from brain.exceptions import BrainError
from brain.models import Entity, EntityAliasSource, PageType
from brain.pages import parse_page, regenerate_index, update_sources
from brain.pipeline.rebuild import page_entity_type
from brain.transactions import durable_unit


def _fingerprint(root: Path) -> str:
    selected = [
        root / "brain.db",
        root / "brain.db-wal",
        root / "config.toml",
        root / "events.jsonl",
    ]
    for name in ("pages", "review", "laundry"):
        selected.extend(sorted((root / name).rglob("*.md")))
    hashes = {
        p.relative_to(root).as_posix(): file_hash(p)
        for p in selected
        if p.is_file() and not (p.name == "brain.db-wal" and p.stat().st_size == 0)
    }
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def plan_reconcile(root: Path) -> dict:
    with root_lock(root), closing(connect(root / "brain.db", read_only=True)) as conn:
        return _plan(root, conn)


def _plan(root: Path, conn) -> dict:
    plan: dict = {
        "version": 1,
        "fingerprint": _fingerprint(root),
        "registry": [],
        "sources": [],
        "alias_conflicts": [],
        "suspect_types": [],
        "missing_pages": [],
        "merge_candidates": [],
        "summary_candidates": [],
        "metadata_leaks": [],
        "raw_review_copies": [],
        "cleanup_candidates": [],
        "aliases": [],
        "errors": [],
    }
    entities = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM entities")}
    from brain.pipeline.resolve import normalize_name

    alias_owners = {
        normalize_name(row["alias"]): row["entity_id"]
        for row in conn.execute("SELECT alias, entity_id FROM entity_aliases")
    }
    pages = {}
    stub_count = 0
    all_pages = []
    for path in sorted((root / "pages").rglob("*.md")):
        if path.name in {"index.md", "log.md"}:
            continue
        try:
            page = parse_page(path)
        except BrainError as exc:
            plan["errors"].append({"path": path.relative_to(root).as_posix(), "error": str(exc)})
            continue
        all_pages.append((path, page))
        slug = page.frontmatter.slug
        if slug in pages:
            plan["errors"].append({"duplicate_slug": slug})
        pages[slug] = path
        if "(stub - waiting for more evidence)" in page.compiled_truth:
            stub_count += 1
            plan["summary_candidates"].append(path.relative_to(root).as_posix())
        if re.search(r"(?m)^source_(?:agent|context):", page.compiled_truth):
            plan["metadata_leaks"].append(path.relative_to(root).as_posix())
        if page.frontmatter.type is not PageType.PROCEDURE:
            existing = get_entity(conn, slug)
            desired = {
                "id": slug,
                "type": page_entity_type(page, existing).value,
                "title": page.frontmatter.title,
                "page_path": path.relative_to(root).as_posix(),
            }
            before = {key: entities[slug][key] for key in desired} if slug in entities else None
            if before != desired:
                plan["registry"].append({"before": before, "after": desired})
            for alias in page.frontmatter.aliases:
                owner = lookup_by_alias(conn, alias)
                normalized_owner = alias_owners.get(normalize_name(alias))
                if normalized_owner is not None and normalized_owner != slug:
                    plan["alias_conflicts"].append(
                        {"alias": alias, "owner": normalized_owner, "page": slug}
                    )
                    continue
                alias_owners[normalize_name(alias)] = slug
                if owner is not None and owner != slug:
                    plan["alias_conflicts"].append({"alias": alias, "owner": owner, "page": slug})
                elif owner is None:
                    plan["aliases"].append({"alias": alias, "entity_id": slug})
        for source in page.sources:
            if not source.startswith("laundry/") or (root / source).exists():
                continue
            name = Path(source).name
            stem, suffix = Path(name).stem, Path(name).suffix
            candidates = [
                p
                for p in (root / "laundry" / "processed").rglob("*")
                if p.is_file()
                and (
                    p.name == name
                    or re.fullmatch(re.escape(stem) + r"_\d+" + re.escape(suffix), p.name)
                )
            ]
            plan["sources"].append(
                {
                    "page": path.relative_to(root).as_posix(),
                    "old": source,
                    "new": candidates[0].relative_to(root).as_posix()
                    if len(candidates) == 1
                    else None,
                    "candidates": [p.relative_to(root).as_posix() for p in candidates],
                }
            )
    for slug, entity in entities.items():
        if entity["type"] == "person":
            plan["suspect_types"].append(
                {
                    "id": slug,
                    "page_path": entity["page_path"],
                    "action": "inspect evidence; never infer non-person from name alone",
                }
            )
        if not entity["page_path"] or not (root / entity["page_path"]).is_file():
            fact_count = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE subject = ? OR (object_type = 'entity' AND object = ?)",
                (slug, slug),
            ).fetchone()[0]
            plan["missing_pages"].append(
                {"id": slug, "facts": fact_count, "action": "retain; no deletion"}
            )
    # Exact normalized title collisions only; semantic similarity is not proof of identity.
    by_title: dict[str, set[str]] = {}
    for slug, entity in entities.items():
        by_title.setdefault(normalize_name(entity["title"]), set()).add(slug)
    for _, page in all_pages:
        by_title.setdefault(normalize_name(page.frontmatter.title), set()).add(
            page.frontmatter.slug
        )
    plan["merge_candidates"] = [sorted(group) for group in by_title.values() if len(group) > 1]
    for path in sorted((root / "review").rglob("*.md")):
        if re.search(r'"raw_payload"\s*:\s*"', path.read_text(encoding="utf-8")):
            plan["raw_review_copies"].append(path.relative_to(root).as_posix())
    for path in sorted((root / "laundry").glob("import-*")):
        if path.is_dir() and not any(path.iterdir()):
            plan["cleanup_candidates"].append(path.relative_to(root).as_posix())
    plan["cleanup_candidates"].extend(p.name for p in root.glob("brain.db.backup-*") if p.is_file())
    predicates = Counter(row[0] for row in conn.execute("SELECT predicate FROM facts"))
    from brain.predicates import normalize_predicate

    plan["predicate_candidates"] = [
        {"old": value, "canonical": normalize_predicate(value), "count": count}
        for value, count in sorted(predicates.items())
        if value != normalize_predicate(value)
    ]
    plan["counts"] = {
        "pages": len(all_pages),
        "stub_pages": stub_count,
        "entities": len(entities),
        "facts": conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
        "registry_changes": len(plan["registry"]),
        "aliases_added": len(plan["aliases"]),
        "source_repairs": sum(bool(x["new"]) for x in plan["sources"]),
        "source_ambiguous": sum(not x["new"] for x in plan["sources"]),
    }
    return plan


def apply_reconcile(root: Path, plan: dict, backup: Path) -> dict:
    with root_lock(root, write=True):
        require_current_backup(root, backup)
        with closing(connect(root / "brain.db")) as conn:
            current = _plan(root, conn)
            if plan != current:
                raise BrainError("Plan is stale or was modified; create and review a fresh dry-run")
            if plan["errors"] or plan["alias_conflicts"]:
                raise BrainError("Resolve page or alias ambiguities before applying reconciliation")
            with durable_unit(root, conn, "reconcile:" + plan["fingerprint"]):
                for change in plan["registry"]:
                    desired = change["after"]
                    page = parse_page(root / desired["page_path"])
                    existing = get_entity(conn, desired["id"])
                    if existing:
                        entity = existing.model_copy(
                            update={
                                "type": page_entity_type(page, existing),
                                "title": desired["title"],
                                "page_path": desired["page_path"],
                            }
                        )
                    else:
                        entity = Entity(
                            **desired,
                            first_seen=page.frontmatter.created,
                            last_seen=page.frontmatter.updated,
                        )
                    upsert_entity(conn, entity)
                for alias in plan["aliases"]:
                    add_alias(
                        conn, alias["alias"], alias["entity_id"], EntityAliasSource.FRONTMATTER
                    )
                for change in plan["sources"]:
                    if not change["new"]:
                        continue
                    path = root / change["page"]
                    page = parse_page(path)
                    update_sources(
                        path,
                        [
                            change["new"] if value == change["old"] else value
                            for value in page.sources
                        ],
                    )
                    conn.execute(
                        "UPDATE facts SET source_ref = ? WHERE source_ref = ?",
                        (change["new"], change["old"]),
                    )
                regenerate_index(root)
    return {
        "applied": True,
        "registry": len(plan["registry"]),
        "sources": plan["counts"]["source_repairs"],
    }
