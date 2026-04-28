from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from brain.config import load_config
from brain.exceptions import ConfigError, LLMError
from brain.llm.prompts import (
    build_compiled_truth_prompt,
    build_conflict_prompt,
    build_question_answer_prompt,
    build_signal_extraction_prompt,
)
from brain.models.fact import Fact, FactCandidate
from brain.pages.timeline import TimelineEntry

if TYPE_CHECKING:
    from brain.pipeline.signal_detect import SignalExtraction


BRAIN_CONFIG_ENV = "BRAIN_CONFIG"
DEFAULT_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-latest"
MAX_OUTPUT_TOKENS = 4096


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


def extract_signal(text: str) -> SignalExtraction:
    """Extract structured signals from text using the configured LLM."""
    from brain.pipeline.signal_detect import SignalExtraction

    prompt = build_signal_extraction_prompt(text)
    data = _request_structured_json(prompt)
    return SignalExtraction.model_validate(data)


def judge_conflict(old: Fact, new: FactCandidate) -> ConflictJudgment:
    """Judge whether a new fact candidate conflicts with an existing fact."""
    prompt = build_conflict_prompt(
        old.model_dump(mode="json"),
        new.model_dump(mode="json"),
    )
    data = _request_structured_json(prompt)
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
    data = _request_structured_json(prompt)
    return _CompiledTruthRewrite.model_validate(data).compiled_truth


def answer_question(query: str, pages: list[dict[str, Any]]) -> QuestionAnswer:
    """Answer a user question using only retrieved brain page evidence."""
    prompt = build_question_answer_prompt(query, pages)
    data = _request_structured_json(prompt)
    return QuestionAnswer.model_validate(data)


def _request_structured_json(prompt: str) -> Any:
    response = _invoke_with_retry(prompt)
    return _parse_structured_response(response)


def _invoke_with_retry(prompt: str) -> Any:
    last_exc: Exception | None = None
    for _ in range(2):
        try:
            return _extract_impl(prompt)
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


def _extract_impl(prompt: str) -> str:
    """Call Anthropic and return the model's text response.

    Tests should monkeypatch this function; public helpers are responsible for retry,
    structured JSON parsing, and schema validation.
    """
    from anthropic import Anthropic

    settings = _resolve_anthropic_settings()
    client_kwargs = {"api_key": settings.api_key} if settings.api_key else {}
    client = Anthropic(**client_kwargs)
    message = client.messages.create(
        model=settings.model,
        max_tokens=MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_text_from_message(message)


def _resolve_anthropic_settings() -> _AnthropicSettings:
    config_path = os.environ.get(BRAIN_CONFIG_ENV)
    if config_path:
        return _settings_from_config(Path(config_path))

    cwd_config = Path.cwd() / "config.toml"
    if cwd_config.exists():
        return _settings_from_config(cwd_config)

    return _AnthropicSettings(
        model=os.environ.get("BRAIN_ANTHROPIC_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or DEFAULT_ANTHROPIC_MODEL,
        api_key=os.environ.get(DEFAULT_ANTHROPIC_API_KEY_ENV),
    )


def _settings_from_config(path: Path) -> _AnthropicSettings:
    try:
        config = load_config(path)
    except ConfigError as exc:
        raise LLMError("Could not load LLM config") from exc

    return _AnthropicSettings(
        model=config.anthropic.model,
        api_key=os.environ.get(config.anthropic.api_key_env),
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
