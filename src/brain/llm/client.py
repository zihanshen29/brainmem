from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from brain.config import AnthropicConfig, DeepSeekConfig, OpenAIConfig, load_config
from brain.exceptions import ConfigError, LLMError
from brain.llm.prompts import (
    build_compiled_truth_prompt,
    build_conflict_prompt,
    build_promote_chat_prompt,
    build_question_answer_prompt,
    build_signal_extraction_prompt,
)
from brain.models.fact import Fact, FactCandidate
from brain.pages.timeline import TimelineEntry

if TYPE_CHECKING:
    from brain.pipeline.signal_detect import SignalExtraction


BRAIN_CONFIG_ENV = "BRAIN_CONFIG"
DEFAULT_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_OPENAI_MODEL = "gpt-5.5"
DEFAULT_OPENAI_FAST_MODEL = "gpt-5.4-mini"
DEFAULT_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-latest"
DEFAULT_ANTHROPIC_FAST_MODEL = "claude-3-5-haiku-latest"
DEFAULT_DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_FAST_MODEL = "deepseek-v4-flash"
MAX_OUTPUT_TOKENS = 4096


@dataclass(frozen=True)
class _LLMSettings:
    provider: str
    model: str
    api_key: str | None
    fast_model: str
    base_url: str | None = None

    def selected_model(self, *, use_fast: bool) -> str:
        return self.fast_model if use_fast else self.model


@dataclass(frozen=True)
class _AnthropicSettings:
    model: str
    api_key: str | None


class ConflictJudgment(BaseModel):
    """LLM judgment for whether a candidate fact conflicts with an existing fact."""

    model_config = ConfigDict(extra="forbid")

    is_conflict: bool = Field(
        validation_alias=AliasChoices("is_conflict", "conflicts", "has_conflict")
    )
    new_supersedes_old: bool = Field(
        validation_alias=AliasChoices(
            "new_supersedes_old",
            "new_supersedes",
            "supersedes_old",
            "supersedes",
        )
    )
    reason: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("reason", "rationale", "explanation"),
    )
    confidence: float = Field(..., ge=0.0, le=1.0)


class _CompiledTruthRewrite(BaseModel):
    """Structured response for compiled truth rewrites."""

    model_config = ConfigDict(extra="forbid")

    compiled_truth: str


class QuestionAnswer(BaseModel):
    """Structured answer generated from retrieved brain pages."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(..., min_length=1)
    sources: list[str] = Field(default_factory=list)


class PromotedChatDraft(BaseModel):
    """Structured draft for a promoted AI chat conversation page."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    compiled_truth: str = Field(..., min_length=1)
    timeline_description: str = Field(..., min_length=1)


def extract_signal(text: str) -> SignalExtraction:
    """Extract structured signals from text using the configured LLM."""
    from brain.pipeline.signal_detect import SignalExtraction

    prompt = build_signal_extraction_prompt(text)
    data = _request_structured_json(prompt, use_fast=True)
    return SignalExtraction.model_validate(data)


def judge_conflict(old: Fact, new: FactCandidate) -> ConflictJudgment:
    """Judge whether a new fact candidate conflicts with an existing fact."""
    prompt = build_conflict_prompt(
        old.model_dump(mode="json"),
        new.model_dump(mode="json"),
    )
    data = _request_structured_json(prompt, use_fast=False)
    return ConflictJudgment.model_validate(data)


def rewrite_compiled_truth(
    timeline: list[TimelineEntry],
    current_truth: str | None,
) -> str:
    """Rewrite compiled truth text from timeline evidence."""
    prompt = build_compiled_truth_prompt(
        [entry.model_dump(mode="json") for entry in timeline],
        current_truth,
    )
    data = _request_structured_json(prompt, use_fast=False)
    return _CompiledTruthRewrite.model_validate(data).compiled_truth


def answer_question(query: str, pages: list[dict[str, Any]]) -> QuestionAnswer:
    """Answer a user question using only retrieved brain page evidence."""
    prompt = build_question_answer_prompt(query, pages)
    data = _request_structured_json(prompt, use_fast=True)
    return QuestionAnswer.model_validate(data)


def promote_chat(
    raw_text: str,
    title_hint: str | None = None,
    slug_hint: str | None = None,
) -> PromotedChatDraft:
    """Draft a conversation page from raw AI chat text using the configured LLM."""
    prompt = build_promote_chat_prompt(raw_text, title_hint=title_hint, slug_hint=slug_hint)
    data = _request_structured_json(prompt, use_fast=False)
    return PromotedChatDraft.model_validate(data)


def _request_structured_json(prompt: str, use_fast: bool = False) -> Any:
    response = _invoke_with_retry(prompt, use_fast=use_fast)
    return _parse_structured_response(response)


def _invoke_with_retry(prompt: str, *, use_fast: bool) -> Any:
    last_exc: Exception | None = None
    for _ in range(2):
        try:
            return _extract_impl(prompt, use_fast=use_fast)
        except LLMError:
            raise
        except Exception as exc:
            last_exc = exc

    if last_exc is None:
        raise LLMError("LLM API call failed")
    raise LLMError("LLM API call failed") from last_exc


def _parse_structured_response(response: Any) -> Any:
    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError as exc:
            raise LLMError("LLM response was not valid JSON") from exc
        if isinstance(parsed, dict | list):
            return parsed
        raise LLMError("LLM response was not structured JSON")

    if isinstance(response, dict | list):
        return response

    raise LLMError("LLM response was not structured JSON")


def _extract_impl(prompt: str, use_fast: bool = False) -> str:
    """Call the configured LLM provider and return the model's text response.

    Tests should monkeypatch this function; public helpers are responsible for retry,
    structured JSON parsing, and schema validation.
    """
    settings = _resolve_llm_settings()
    if settings.provider == "deepseek":
        return _extract_deepseek(prompt, settings, use_fast=use_fast)
    if settings.provider == "openai":
        return _extract_openai(prompt, settings, use_fast=use_fast)
    if settings.provider == "anthropic":
        return _extract_anthropic(prompt, settings, use_fast=use_fast)
    raise LLMError(f"Unsupported LLM provider: {settings.provider}")


def _resolve_anthropic_settings() -> _AnthropicSettings:
    settings = _resolve_llm_settings(preferred_provider="anthropic")
    if settings.provider != "anthropic":
        raise LLMError(f"Configured LLM provider is not Anthropic: {settings.provider}")
    return _AnthropicSettings(model=settings.model, api_key=settings.api_key)


def _resolve_llm_settings(preferred_provider: str | None = None) -> _LLMSettings:
    config_path = os.environ.get(BRAIN_CONFIG_ENV)
    if config_path:
        return _settings_from_config(Path(config_path), preferred_provider=preferred_provider)

    cwd_config = Path.cwd() / "config.toml"
    if cwd_config.exists():
        return _settings_from_config(cwd_config, preferred_provider=preferred_provider)

    if preferred_provider == "anthropic":
        return _default_anthropic_settings()
    if preferred_provider == "openai":
        return _default_openai_settings()
    return _default_deepseek_settings()


def _extract_openai(prompt: str, settings: _LLMSettings, *, use_fast: bool) -> str:
    from openai import OpenAI

    client_kwargs = {"api_key": settings.api_key} if settings.api_key else {}
    client = OpenAI(**client_kwargs)
    response = client.responses.create(
        model=settings.selected_model(use_fast=use_fast),
        input=prompt,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    text = getattr(response, "output_text", None)
    if not isinstance(text, str) or not text.strip():
        raise LLMError("LLM response did not contain text")
    return text


def _extract_deepseek(prompt: str, settings: _LLMSettings, *, use_fast: bool) -> str:
    from openai import OpenAI

    client_kwargs: dict[str, str] = {}
    if settings.api_key:
        client_kwargs["api_key"] = settings.api_key
    if settings.base_url:
        client_kwargs["base_url"] = settings.base_url

    client = OpenAI(**client_kwargs)
    response = client.chat.completions.create(
        model=settings.selected_model(use_fast=use_fast),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=MAX_OUTPUT_TOKENS,
        stream=False,
    )
    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise LLMError("LLM response did not contain text")
    return content


def _extract_anthropic(prompt: str, settings: _LLMSettings, *, use_fast: bool) -> str:
    from anthropic import Anthropic

    client_kwargs = {"api_key": settings.api_key} if settings.api_key else {}
    client = Anthropic(**client_kwargs)
    message = client.messages.create(
        model=settings.selected_model(use_fast=use_fast),
        max_tokens=MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_text_from_message(message)


def _default_openai_settings() -> _LLMSettings:
    return _LLMSettings(
        provider="openai",
        model=os.environ.get("BRAIN_OPENAI_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or DEFAULT_OPENAI_MODEL,
        api_key=os.environ.get(DEFAULT_OPENAI_API_KEY_ENV),
        fast_model=os.environ.get("BRAIN_OPENAI_FAST_MODEL")
        or os.environ.get("OPENAI_FAST_MODEL")
        or DEFAULT_OPENAI_FAST_MODEL,
        base_url=None,
    )


def _default_anthropic_settings() -> _LLMSettings:
    return _LLMSettings(
        provider="anthropic",
        model=os.environ.get("BRAIN_ANTHROPIC_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or DEFAULT_ANTHROPIC_MODEL,
        api_key=os.environ.get(DEFAULT_ANTHROPIC_API_KEY_ENV),
        fast_model=os.environ.get("BRAIN_ANTHROPIC_FAST_MODEL")
        or os.environ.get("ANTHROPIC_FAST_MODEL")
        or DEFAULT_ANTHROPIC_FAST_MODEL,
        base_url=None,
    )


def _default_deepseek_settings() -> _LLMSettings:
    return _LLMSettings(
        provider="deepseek",
        model=os.environ.get("BRAIN_DEEPSEEK_MODEL")
        or os.environ.get("DEEPSEEK_MODEL")
        or DEFAULT_DEEPSEEK_MODEL,
        api_key=os.environ.get(DEFAULT_DEEPSEEK_API_KEY_ENV),
        fast_model=os.environ.get("BRAIN_DEEPSEEK_FAST_MODEL")
        or os.environ.get("DEEPSEEK_FAST_MODEL")
        or DEFAULT_DEEPSEEK_FAST_MODEL,
        base_url=os.environ.get("BRAIN_DEEPSEEK_BASE_URL")
        or os.environ.get("DEEPSEEK_BASE_URL")
        or DEFAULT_DEEPSEEK_BASE_URL,
    )


def _settings_from_config(
    path: Path,
    *,
    preferred_provider: str | None = None,
) -> _LLMSettings:
    try:
        config = load_config(path)
    except ConfigError as exc:
        raise LLMError("Could not load LLM config") from exc

    provider = preferred_provider
    if provider is None:
        if config.deepseek is not None:
            provider = "deepseek"
        elif config.openai is not None:
            provider = "openai"
        else:
            provider = "anthropic"

    if provider == "deepseek" and config.deepseek is not None:
        return _settings_from_section(
            provider="deepseek",
            section=config.deepseek,
        )
    if provider == "openai" and config.openai is not None:
        return _settings_from_section(
            provider="openai",
            section=config.openai,
        )
    if provider == "anthropic" and config.anthropic is not None:
        return _settings_from_section(
            provider="anthropic",
            section=config.anthropic,
        )

    if preferred_provider:
        raise LLMError(f"Config does not contain [{preferred_provider}] LLM settings")
    raise LLMError("Config does not contain LLM settings")


def _settings_from_section(
    *,
    provider: str,
    section: DeepSeekConfig | OpenAIConfig | AnthropicConfig,
) -> _LLMSettings:
    return _LLMSettings(
        provider=provider,
        model=section.model,
        api_key=os.environ.get(section.api_key_env),
        fast_model=section.fast_model,
        base_url=section.base_url if isinstance(section, DeepSeekConfig) else None,
    )


def _extract_text_from_message(message: Any) -> str:
    text_parts: list[str] = []
    for block in getattr(message, "content", []):
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if isinstance(text, str):
            text_parts.append(text)

    text = "\n".join(text_parts).strip()
    if not text:
        raise LLMError("LLM response did not contain text")
    return text
