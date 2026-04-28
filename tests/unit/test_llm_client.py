from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from brain.exceptions import LLMError
from brain.llm import client as llm_client
from brain.llm.client import ConflictJudgment, PromotedChatDraft
from brain.models.entity import EntityType
from brain.models.fact import Fact, FactCandidate, FactObjectType
from brain.models.page import PageType
from brain.pages.timeline import TimelineEntry
from brain.pipeline.signal_detect import SignalExtraction

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"


def utc_datetime() -> datetime:
    return datetime(2026, 4, 28, 12, 0, tzinfo=UTC)


def sample_fact() -> Fact:
    return Fact(
        id=1,
        subject="zihan",
        predicate="location",
        object="UK",
        object_type=FactObjectType.LITERAL,
        valid_from="2024-09-01",
        asserted_at=utc_datetime(),
        source_event=VALID_ULID,
        confidence=0.9,
    )


def sample_candidate() -> FactCandidate:
    return FactCandidate(
        subject="zihan",
        predicate="location",
        object="US",
        object_type=FactObjectType.LITERAL,
        valid_from="2026-04-28",
        source_event=VALID_ULID,
        confidence=0.8,
    )


def signal_payload() -> dict:
    return {
        "entities": [
            {
                "name": "Zihan",
                "type": "person",
                "confidence": 0.94,
                "metadata": {"source": "test"},
            }
        ],
        "facts": [sample_candidate().model_dump(mode="json")],
        "timeline_summary": "Zihan is now in the US.",
        "suggested_page_type": "entity",
    }


def valid_config_text(root: Path) -> str:
    return f"""
[anthropic]
api_key_env = "CUSTOM_ANTHROPIC_API_KEY"
model = "claude-config-model"
fast_model = "claude-fast-model"

[paths]
brain_root = "{root.as_posix()}"

[ingest]
confidence_auto_accept = 0.85
confidence_auto_reject = 0.50

[tier]
tier3_threshold = 1
tier2_threshold = 3
tier1_threshold = 8

[lint]
stale_days = 90

[git]
auto_commit = true
""".strip()


def test_extract_signal_happy_path_returns_signal_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt: signal_payload())

    result = llm_client.extract_signal("Zihan is in the US.")

    assert isinstance(result, SignalExtraction)
    assert result.entities[0].type is EntityType.PERSON
    assert result.facts == [sample_candidate()]
    assert result.suggested_page_type is PageType.ENTITY


def test_extract_signal_invalid_json_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt: "{not valid json")

    with pytest.raises(LLMError):
        llm_client.extract_signal("Zihan is in the US.")


def test_extract_signal_schema_invalid_preserves_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt: {"facts": [{"subject": ""}]})

    with pytest.raises(ValidationError):
        llm_client.extract_signal("Zihan is in the US.")


def test_judge_conflict_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm_client,
        "_extract_impl",
        lambda prompt: (
            '{"is_conflict": true, "new_supersedes_old": true, '
            '"reason": "newer location", "confidence": 0.91}'
        ),
    )

    judgment = llm_client.judge_conflict(sample_fact(), sample_candidate())

    assert judgment == ConflictJudgment(
        is_conflict=True,
        new_supersedes_old=True,
        reason="newer location",
        confidence=0.91,
    )


def test_rewrite_compiled_truth_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm_client,
        "_extract_impl",
        lambda prompt: {"compiled_truth": "Zihan is currently in the US."},
    )
    timeline = [
        TimelineEntry(
            date="2026-04-28",
            event_id=VALID_ULID,
            description="Zihan moved to the US.",
        )
    ]

    result = llm_client.rewrite_compiled_truth(timeline, "Zihan is in the UK.")

    assert result == "Zihan is currently in the US."


def test_answer_question_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def fake_extract(prompt: str) -> dict[str, object]:
        prompts.append(prompt)
        return {
            "answer": "Alice maintains Brain and is working on the ask CLI.",
            "sources": ["alice", "brain-ask"],
        }

    monkeypatch.setattr(llm_client, "_extract_impl", fake_extract)

    result = llm_client.answer_question(
        "What is Alice working on?",
        [
            {
                "slug": "alice",
                "title": "Alice",
                "compiled_truth": "Alice maintains Brain.",
                "timeline": ["- 2026-04-28 [event:01KQA8R9KVCG906A0203VYEQF7]: Ask CLI work."],
            }
        ],
    )

    assert result.answer == "Alice maintains Brain and is working on the ask CLI."
    assert result.sources == ["alice", "brain-ask"]
    assert "What is Alice working on?" in prompts[0]
    assert "Return an object with keys: answer, sources." in prompts[0]


def test_answer_question_invalid_json_raises_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt: "{not valid json")

    with pytest.raises(LLMError):
        llm_client.answer_question("What is Alice doing?", [])


def test_answer_question_schema_invalid_preserves_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt: {"sources": []})

    with pytest.raises(ValidationError):
        llm_client.answer_question("What is Alice doing?", [])


def test_promote_chat_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def fake_extract(prompt: str) -> dict[str, str]:
        prompts.append(prompt)
        return {
            "title": "Architecture Review",
            "compiled_truth": "The chat captured a durable architecture decision.",
            "timeline_description": "Captured an architecture decision.",
        }

    monkeypatch.setattr(llm_client, "_extract_impl", fake_extract)

    result = llm_client.promote_chat(
        "User: Raw conversation content.",
        title_hint="Hint Title",
        slug_hint="hint-slug",
    )

    assert result == PromotedChatDraft(
        title="Architecture Review",
        compiled_truth="The chat captured a durable architecture decision.",
        timeline_description="Captured an architecture decision.",
    )
    assert "Return only valid JSON" in prompts[0]
    assert "User: Raw conversation content." in prompts[0]
    assert "Hint Title" in prompts[0]
    assert "hint-slug" in prompts[0]


def test_promote_chat_invalid_json_raises_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt: "{not valid json")

    with pytest.raises(LLMError):
        llm_client.promote_chat("User: Raw conversation content.")


def test_promote_chat_schema_invalid_preserves_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_client,
        "_extract_impl",
        lambda prompt: {"title": "Missing required fields"},
    )

    with pytest.raises(ValidationError):
        llm_client.promote_chat("User: Raw conversation content.")


def test_api_exception_retries_once_then_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fail(prompt: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(llm_client, "_extract_impl", fail)

    with pytest.raises(LLMError) as exc_info:
        llm_client.judge_conflict(sample_fact(), sample_candidate())

    assert calls == 2
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_anthropic_settings_read_model_and_key_from_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(valid_config_text(tmp_path / "brain"), encoding="utf-8", newline="\n")
    monkeypatch.setenv(llm_client.BRAIN_CONFIG_ENV, str(config_path))
    monkeypatch.setenv("CUSTOM_ANTHROPIC_API_KEY", "secret-key")

    settings = llm_client._resolve_anthropic_settings()

    assert settings.model == "claude-config-model"
    assert settings.api_key == "secret-key"
