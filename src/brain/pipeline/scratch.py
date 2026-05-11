from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from brain.exceptions import BrainError
from brain.ledger.reader import read_all
from brain.paths import BrainPaths

ENTRY_HEADER_RE = re.compile(r"^## (?P<timestamp>\S+) - source: (?P<source>.+)$")


class ScratchAppendReport(BaseModel):
    """Summary of one append to the working scratch buffer."""

    model_config = ConfigDict(extra="forbid")

    path: str
    entries: int
    char_count: int
    created: bool
    summary: str


class SnapshotRebuildReport(BaseModel):
    """Summary of one deterministic scratch snapshot rebuild."""

    model_config = ConfigDict(extra="forbid")

    path: str
    entries: int
    char_count: int
    created: bool
    updated: bool
    summary: str


class ScratchEntry(BaseModel):
    """Parsed entry from scratch/working.md."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str
    source: str
    text: str


def append_working(
    brain_root: Path,
    text: str,
    source: str = "manual",
    timestamp: datetime | str | None = None,
) -> ScratchAppendReport:
    """Append one local scratch item to scratch/working.md."""
    body = _normalize_text(text).strip()
    if not body:
        raise BrainError("Scratch content is empty")

    paths = BrainPaths(Path(brain_root))
    path = paths.working_buffer
    created = not path.exists()
    rendered = _render_working_entry(body, source, _format_timestamp(timestamp))

    path.parent.mkdir(parents=True, exist_ok=True)
    if created:
        path.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)

    return ScratchAppendReport(
        path=_relative_path(path, paths.root),
        entries=1,
        char_count=len(body),
        created=created,
        summary=f"Appended 1 scratch entry ({len(body)} chars)",
    )


def rebuild_snapshot(
    brain_root: Path,
    max_items: int = 20,
    max_chars: int = 8000,
    title: str | None = None,
    timestamp: datetime | str | None = None,
) -> SnapshotRebuildReport:
    """Build scratch/SNAPSHOT.md from recent working scratch entries."""
    if max_items <= 0:
        raise BrainError("max_items must be positive")
    if max_chars <= 0:
        raise BrainError("max_chars must be positive")

    paths = BrainPaths(Path(brain_root))
    working_path = paths.working_buffer
    if not working_path.exists():
        raise BrainError(f"Working scratch buffer not found: {working_path}")

    entries = _parse_working_entries(working_path.read_text(encoding="utf-8"))
    selected = _select_recent_entries(entries, max_items=max_items, max_chars=max_chars)
    snapshot = _render_snapshot(
        selected,
        title=title or "Scratch Snapshot",
        timestamp=_format_timestamp(timestamp),
        max_items=max_items,
        max_chars=max_chars,
        procedure_runs=_recent_procedure_runs(paths),
    )

    path = paths.snapshot_path
    created = not path.exists()
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot, encoding="utf-8", newline="\n")
    updated = previous != snapshot

    char_count = sum(len(entry.text) for entry in selected)
    return SnapshotRebuildReport(
        path=_relative_path(path, paths.root),
        entries=len(selected),
        char_count=char_count,
        created=created,
        updated=updated,
        summary=f"Rebuilt snapshot with {len(selected)} entries ({char_count} chars)",
    )


def _render_working_entry(text: str, source: str, timestamp: str) -> str:
    source_label = _normalize_source(source)
    return f"\n## {timestamp} - source: {source_label}\n\n{text}\n"


def _render_snapshot(
    entries: list[ScratchEntry],
    title: str,
    timestamp: str,
    max_items: int,
    max_chars: int,
    procedure_runs: list[str] | None = None,
) -> str:
    lines = [
        f"# {title.strip() or 'Scratch Snapshot'}",
        "",
        f"Generated: {timestamp}",
        "Strategy: recent-items",
        f"Limits: max_items={max_items}, max_chars={max_chars}",
        f"Entries: {len(entries)}",
        "",
    ]
    for entry in entries:
        lines.extend(
            [
                f"## {entry.timestamp} - source: {entry.source}",
                "",
                entry.text,
                "",
            ]
        )
    if procedure_runs:
        lines.extend(["## Recent procedure runs", ""])
        lines.extend(f"- {run}" for run in procedure_runs)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _recent_procedure_runs(paths: BrainPaths, limit: int = 5) -> list[str]:
    if not paths.events_jsonl.exists():
        return []
    runs: list[str] = []
    for event in reversed(list(read_all(paths.events_jsonl))):
        if event.metadata.get("operation") != "run":
            continue
        procedure = event.metadata.get("procedure")
        result = event.metadata.get("result")
        if not procedure or not result:
            continue
        note = (event.raw_payload or "").strip()
        runs.append(f"{event.timestamp.date().isoformat()} {procedure} {result}: {note}")
        if len(runs) >= limit:
            break
    return runs


def _parse_working_entries(text: str) -> list[ScratchEntry]:
    entries: list[ScratchEntry] = []
    current_timestamp: str | None = None
    current_source: str | None = None
    current_lines: list[str] = []

    for line in _normalize_text(text).split("\n"):
        match = ENTRY_HEADER_RE.match(line)
        if match:
            if current_timestamp is not None and current_source is not None:
                entries.append(
                    ScratchEntry(
                        timestamp=current_timestamp,
                        source=current_source,
                        text="\n".join(current_lines).strip(),
                    )
                )
            current_timestamp = match.group("timestamp")
            current_source = match.group("source").strip()
            current_lines = []
        elif current_timestamp is not None:
            current_lines.append(line)

    if current_timestamp is not None and current_source is not None:
        entries.append(
            ScratchEntry(
                timestamp=current_timestamp,
                source=current_source,
                text="\n".join(current_lines).strip(),
            )
        )
    return [entry for entry in entries if entry.text]


def _select_recent_entries(
    entries: list[ScratchEntry],
    max_items: int,
    max_chars: int,
) -> list[ScratchEntry]:
    selected_reversed: list[ScratchEntry] = []
    char_count = 0
    for entry in reversed(entries):
        if len(selected_reversed) >= max_items:
            break
        next_count = char_count + len(entry.text)
        if selected_reversed and next_count > max_chars:
            break
        if not selected_reversed and next_count > max_chars:
            selected_reversed.append(
                ScratchEntry(
                    timestamp=entry.timestamp,
                    source=entry.source,
                    text=entry.text[:max_chars].rstrip(),
                )
            )
            break
        selected_reversed.append(entry)
        char_count = next_count
    return list(reversed(selected_reversed))


def _format_timestamp(timestamp: datetime | str | None) -> str:
    if timestamp is None:
        value = datetime.now(UTC)
    elif isinstance(timestamp, str):
        return timestamp
    else:
        value = timestamp

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_source(source: str) -> str:
    source = _normalize_text(source).strip()
    return source or "manual"


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
