import json
from pathlib import Path

from brain.models.event import Event


def append_event(path: Path, event: Event) -> None:
    """Append one event to the JSONL ledger."""
    from brain.transactions import atomic_text
    line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n"
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    # Preserve append-only contents while avoiding a torn final JSON record.
    atomic_text(path, before + line)
