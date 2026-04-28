import json
import logging
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from brain.models.event import Event


def read_all(path: Path) -> Iterator[Event]:
    """Yield every valid event from the ledger in file order."""
    try:
        with path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                try:
                    data = json.loads(line)
                    yield Event.model_validate(data)
                except json.JSONDecodeError as exc:
                    logging.warning(
                        "Skipping corrupt JSON ledger row %s:%s: %s",
                        path,
                        line_number,
                        exc,
                    )
                except ValidationError as exc:
                    logging.warning(
                        "Skipping invalid event ledger row %s:%s: %s",
                        path,
                        line_number,
                        exc,
                    )
    except FileNotFoundError:
        return


def read_after(path: Path, last_id: str) -> Iterator[Event]:
    """Yield valid events with ULID ids strictly greater than last_id."""
    for event in read_all(path):
        if event.id > last_id:
            yield event


def find_event(path: Path, id: str) -> Event | None:
    """Return the event with id, or None if it is absent."""
    for event in read_all(path):
        if event.id == id:
            return event
    return None
