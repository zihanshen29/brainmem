from typing import Any

import pytest
from pydantic import ValidationError

from brain.llm import client as llm_client
from brain.models import EntityType, FactObjectType, PageType
from brain.pipeline import SignalExtraction, detect_signal
from brain.pipeline.signal_detect import SignalEntity


def install_fake_llm(monkeypatch: pytest.MonkeyPatch, extract_signal) -> None:
    monkeypatch.setattr(llm_client, "extract_signal", extract_signal)


def extraction_payload() -> dict[str, Any]:
    return {
        "entities": [
            {
                "name": "Alice",
                "type": "person",
                "confidence": 0.93,
                "metadata": {"role": "collaborator"},
            }
        ],
        "facts": [
            {
                "subject": "Alice",
                "predicate": "works_on",
                "object": "Brain",
                "object_type": "entity",
                "valid_from": "2026-04-28",
                "valid_to": None,
                "source_event": "01KQA8R9KVCG906A0203VYEQF7",
                "source_ref": "laundry/alice.md",
                "confidence": 0.88,
            }
        ],
        "timeline_summary": "Alice started working on Brain.",
        "suggested_page_type": "project",
    }


def test_detect_signal_returns_round_trippable_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_extract_signal(text: str) -> dict[str, Any]:
        captured["text"] = text
        return extraction_payload()

    install_fake_llm(monkeypatch, fake_extract_signal)

    result = detect_signal("Alice started working on Brain.")

    assert captured["text"] == "Alice started working on Brain."
    assert isinstance(result, SignalExtraction)
    assert result.entities[0].type is EntityType.PERSON
    assert result.facts[0].object_type is FactObjectType.ENTITY
    assert result.suggested_page_type is PageType.PROJECT
    assert SignalExtraction.model_validate(result.model_dump()) == result


def test_detect_signal_combines_text_and_hint_for_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_extract_signal(text: str) -> dict[str, Any]:
        captured["text"] = text
        return extraction_payload()

    install_fake_llm(monkeypatch, fake_extract_signal)

    detect_signal(
        "Alice started working on Brain.",
        hint={"source_ref": "laundry/alice.md", "expected_page_type": "project"},
    )

    assert "Alice started working on Brain." in captured["text"]
    assert '"source_ref": "laundry/alice.md"' in captured["text"]
    assert '"expected_page_type": "project"' in captured["text"]


def test_signal_extraction_rejects_unknown_fields() -> None:
    payload = extraction_payload()
    payload["unexpected"] = True

    with pytest.raises(ValidationError):
        SignalExtraction.model_validate(payload)


def test_signal_entity_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SignalEntity(
            name="Alice",
            type=EntityType.PERSON,
            confidence=0.93,
            unexpected=True,
        )
