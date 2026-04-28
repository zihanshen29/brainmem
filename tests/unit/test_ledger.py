import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brain.ledger import append_event, find_event, read_after, read_all
from brain.models import Event, EventKind


def sample_event(index: int, event_id: str) -> Event:
    return Event(
        id=event_id,
        timestamp=datetime(2026, 4, 28, 12, 0, tzinfo=UTC) + timedelta(seconds=index),
        kind=EventKind.RAW_IMPORTED,
        source_ref=f"raw/{index}.md",
        raw_payload=f"raw payload {index}",
        extracted_facts=[f"fact {index}"],
        affected_pages=[f"page-{index}"],
        metadata={"index": index},
    )


def generated_events(count: int) -> list[Event]:
    return [
        sample_event(index, f"01KQA8R9KVCG906A0203VY{index:04d}")
        for index in range(count)
    ]


def test_append_and_read_all_preserves_write_order(tmp_path: Path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    events = generated_events(100)

    for event in events:
        append_event(ledger_path, event)

    assert list(read_all(ledger_path)) == events


def test_read_after_uses_strict_ulid_cursor(tmp_path: Path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    events = generated_events(5)

    for event in events:
        append_event(ledger_path, event)

    assert list(read_after(ledger_path, events[2].id)) == events[3:]


def test_find_event_returns_matching_event_or_none(tmp_path: Path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    events = generated_events(3)

    for event in events:
        append_event(ledger_path, event)

    assert find_event(ledger_path, events[1].id) == events[1]
    assert find_event(ledger_path, "01KQA8R9KVCG906A0203VY9999") is None


def test_missing_ledger_reads_as_empty(tmp_path: Path) -> None:
    ledger_path = tmp_path / "events.jsonl"

    assert list(read_all(ledger_path)) == []
    assert list(read_after(ledger_path, "01KQA8R9KVCG906A0203VY0000")) == []
    assert find_event(ledger_path, "01KQA8R9KVCG906A0203VY0000") is None


def test_corrupt_and_invalid_rows_are_skipped(tmp_path: Path, caplog) -> None:
    ledger_path = tmp_path / "events.jsonl"
    valid_before, invalid_event, valid_after = generated_events(3)
    invalid_data = invalid_event.model_dump(mode="json")
    invalid_data["kind"] = "not-a-kind"

    ledger_path.write_text(
        "\n".join(
            [
                json.dumps(valid_before.model_dump(mode="json"), ensure_ascii=False),
                "{not valid json",
                json.dumps(invalid_data, ensure_ascii=False),
                json.dumps(valid_after.model_dump(mode="json"), ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert list(read_all(ledger_path)) == [valid_before, valid_after]
    assert len(caplog.records) == 2
