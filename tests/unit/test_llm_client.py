from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from brain.exceptions import ConfigError, LLMError
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


def openai_config_text(root: Path) -> str:
    return f"""
[openai]
api_key_env = "CUSTOM_OPENAI_API_KEY"
model = "gpt-config-model"
fast_model = "gpt-config-fast-model"

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


def deepseek_config_text(root: Path) -> str:
    return f"""
[deepseek]
api_key_env = "CUSTOM_DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com/custom"
model = "deepseek-config-pro"
fast_model = "deepseek-config-flash"

[openai]
api_key_env = "CUSTOM_OPENAI_API_KEY"
model = "gpt-config-model"
fast_model = "gpt-config-fast-model"

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
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt, use_fast=False: signal_payload())

    result = llm_client.extract_signal("Zihan is in the US.")

    assert isinstance(result, SignalExtraction)
    assert result.entities[0].type is EntityType.PERSON
    assert result.facts == [sample_candidate()]
    assert result.suggested_page_type is PageType.ENTITY


def test_extract_signal_invalid_json_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt, use_fast=False: "{not valid json")

    with pytest.raises(LLMError):
        llm_client.extract_signal("Zihan is in the US.")


def test_extract_signal_schema_invalid_preserves_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_client,
        "_extract_impl",
        lambda prompt, use_fast=False: {"facts": [{"subject": ""}]},
    )

    with pytest.raises(ValidationError):
        llm_client.extract_signal("Zihan is in the US.")


def test_judge_conflict_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm_client,
        "_extract_impl",
        lambda prompt, use_fast=False: (
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
        lambda prompt, use_fast=False: {"compiled_truth": "Zihan is currently in the US."},
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

    def fake_extract(prompt: str, use_fast: bool = False) -> dict[str, object]:
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
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt, use_fast=False: "{not valid json")

    with pytest.raises(LLMError):
        llm_client.answer_question("What is Alice doing?", [])


def test_answer_question_schema_invalid_preserves_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt, use_fast=False: {"sources": []})

    with pytest.raises(ValidationError):
        llm_client.answer_question("What is Alice doing?", [])


def test_promote_chat_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def fake_extract(prompt: str, use_fast: bool = False) -> dict[str, str]:
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
    monkeypatch.setattr(llm_client, "_extract_impl", lambda prompt, use_fast=False: "{not valid json")

    with pytest.raises(LLMError):
        llm_client.promote_chat("User: Raw conversation content.")


def test_promote_chat_schema_invalid_preserves_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_client,
        "_extract_impl",
        lambda prompt, use_fast=False: {"title": "Missing required fields"},
    )

    with pytest.raises(ValidationError):
        llm_client.promote_chat("User: Raw conversation content.")


def test_api_exception_retries_once_then_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fail(prompt: str, use_fast: bool = False) -> dict[str, str]:
        nonlocal calls
        calls += 1
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(llm_client, "_extract_impl", fail)

    with pytest.raises(LLMError) as exc_info:
        llm_client.judge_conflict(sample_fact(), sample_candidate())

    assert calls == 2
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_public_helpers_route_fast_and_pro_models(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    responses: list[dict[str, object]] = [
        signal_payload(),
        {
            "answer": "Alice maintains Brain.",
            "sources": ["alice"],
        },
        {
            "is_conflict": True,
            "new_supersedes_old": True,
            "reason": "newer location",
            "confidence": 0.91,
        },
        {"compiled_truth": "Zihan is currently in the US."},
        {
            "title": "Architecture Review",
            "compiled_truth": "The chat captured a durable architecture decision.",
            "timeline_description": "Captured an architecture decision.",
        },
    ]

    def fake_extract(prompt: str, use_fast: bool = False) -> dict[str, object]:
        calls.append(use_fast)
        return responses.pop(0)

    monkeypatch.setattr(llm_client, "_extract_impl", fake_extract)

    llm_client.extract_signal("Zihan is in the US.")
    llm_client.answer_question("What is Alice doing?", [])
    llm_client.judge_conflict(sample_fact(), sample_candidate())
    llm_client.rewrite_compiled_truth([], None)
    llm_client.promote_chat("User: Raw conversation content.")

    assert calls == [True, True, False, False, False]


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


def test_llm_settings_default_to_deepseek_without_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(llm_client.BRAIN_CONFIG_ENV, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")

    settings = llm_client._resolve_llm_settings()

    assert settings.provider == "deepseek"
    assert settings.model == "deepseek-v4-pro"
    assert settings.fast_model == "deepseek-v4-flash"
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.api_key == "deepseek-secret"


def test_llm_settings_prefer_openai_config_over_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(openai_config_text(tmp_path / "brain"), encoding="utf-8", newline="\n")
    monkeypatch.setenv(llm_client.BRAIN_CONFIG_ENV, str(config_path))
    monkeypatch.setenv("CUSTOM_OPENAI_API_KEY", "openai-config-secret")
    monkeypatch.setenv("CUSTOM_ANTHROPIC_API_KEY", "anthropic-config-secret")

    settings = llm_client._resolve_llm_settings()

    assert settings.provider == "openai"
    assert settings.model == "gpt-config-model"
    assert settings.fast_model == "gpt-config-fast-model"
    assert settings.api_key == "openai-config-secret"


def test_llm_settings_prefer_deepseek_config_over_openai_and_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(deepseek_config_text(tmp_path / "brain"), encoding="utf-8", newline="\n")
    monkeypatch.setenv(llm_client.BRAIN_CONFIG_ENV, str(config_path))
    monkeypatch.setenv("CUSTOM_DEEPSEEK_API_KEY", "deepseek-config-secret")
    monkeypatch.setenv("CUSTOM_OPENAI_API_KEY", "openai-config-secret")
    monkeypatch.setenv("CUSTOM_ANTHROPIC_API_KEY", "anthropic-config-secret")

    settings = llm_client._resolve_llm_settings()

    assert settings.provider == "deepseek"
    assert settings.model == "deepseek-config-pro"
    assert settings.fast_model == "deepseek-config-flash"
    assert settings.base_url == "https://api.deepseek.com/custom"
    assert settings.api_key == "deepseek-config-secret"


def test_llm_settings_fall_back_to_anthropic_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(valid_config_text(tmp_path / "brain"), encoding="utf-8", newline="\n")
    monkeypatch.setenv(llm_client.BRAIN_CONFIG_ENV, str(config_path))
    monkeypatch.setenv("CUSTOM_ANTHROPIC_API_KEY", "anthropic-config-secret")

    settings = llm_client._resolve_llm_settings()

    assert settings.provider == "anthropic"
    assert settings.model == "claude-config-model"
    assert settings.fast_model == "claude-fast-model"
    assert settings.api_key == "anthropic-config-secret"


def test_llm_settings_invalid_config_raises_llm_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[openai]
api_key_env = ""
model = "gpt-config-model"
fast_model = "gpt-config-fast-model"
""".strip(),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setenv(llm_client.BRAIN_CONFIG_ENV, str(config_path))

    with pytest.raises(LLMError) as exc_info:
        llm_client._resolve_llm_settings()

    assert isinstance(exc_info.value.__cause__, ConfigError)


def test_llm_settings_deepseek_only_incomplete_config_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[deepseek]
api_key_env = "CUSTOM_DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com"
model = "deepseek-config-pro"
fast_model = "deepseek-config-flash"
""".strip(),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setenv(llm_client.BRAIN_CONFIG_ENV, str(config_path))

    with pytest.raises(LLMError) as exc_info:
        llm_client._resolve_llm_settings()

    assert isinstance(exc_info.value.__cause__, ConfigError)


def test_extract_impl_dispatches_to_openai_responses_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(openai_config_text(tmp_path / "brain"), encoding="utf-8", newline="\n")
    monkeypatch.setenv(llm_client.BRAIN_CONFIG_ENV, str(config_path))
    monkeypatch.setenv("CUSTOM_OPENAI_API_KEY", "openai-config-secret")
    calls: list[dict[str, object]] = []

    class FakeResponses:
        def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return types.SimpleNamespace(output_text='{"ok": true}')

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            calls.append({"client_kwargs": kwargs})
            self.responses = FakeResponses()

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = llm_client._extract_impl("Return JSON.")

    assert result == '{"ok": true}'
    assert calls == [
        {"client_kwargs": {"api_key": "openai-config-secret"}},
        {
            "model": "gpt-config-model",
            "input": "Return JSON.",
            "max_output_tokens": llm_client.MAX_OUTPUT_TOKENS,
        },
    ]


def test_extract_impl_dispatches_to_deepseek_chat_completions_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(deepseek_config_text(tmp_path / "brain"), encoding="utf-8", newline="\n")
    monkeypatch.setenv(llm_client.BRAIN_CONFIG_ENV, str(config_path))
    monkeypatch.setenv("CUSTOM_DEEPSEEK_API_KEY", "deepseek-config-secret")
    calls: list[dict[str, object]] = []

    class FakeChatCompletions:
        def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content='{"ok": true}'),
                    )
                ]
            )

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeChatCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            calls.append({"client_kwargs": kwargs})
            self.chat = FakeChat()

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = llm_client._extract_impl("Return JSON.", use_fast=True)

    assert result == '{"ok": true}'
    assert calls == [
        {
            "client_kwargs": {
                "api_key": "deepseek-config-secret",
                "base_url": "https://api.deepseek.com/custom",
            }
        },
        {
            "model": "deepseek-config-flash",
            "messages": [{"role": "user", "content": "Return JSON."}],
            "response_format": {"type": "json_object"},
            "max_tokens": llm_client.MAX_OUTPUT_TOKENS,
            "stream": False,
        },
    ]


def test_extract_impl_deepseek_uses_pro_model_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(deepseek_config_text(tmp_path / "brain"), encoding="utf-8", newline="\n")
    monkeypatch.setenv(llm_client.BRAIN_CONFIG_ENV, str(config_path))
    calls: list[dict[str, object]] = []

    class FakeChatCompletions:
        def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content='{"ok": true}'),
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = types.SimpleNamespace(
                completions=FakeChatCompletions(),
            )

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = llm_client._extract_impl("Return JSON.")

    assert result == '{"ok": true}'
    assert calls[0]["model"] == "deepseek-config-pro"
