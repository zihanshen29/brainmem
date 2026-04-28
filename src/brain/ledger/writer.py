import json
from pathlib import Path

from brain.models.event import Event


def append_event(path: Path, event: Event) -> None:
    """Append one event to the JSONL ledger."""
    with open(path, "a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
