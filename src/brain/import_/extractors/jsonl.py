from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any, ClassVar

from brain.exceptions import BrainError
from brain.import_.extractors.base import ExtractedDocument
from brain.models.event import EventKind


class JsonlExtractor:
    """Extract conversation JSONL exports into markdown chat documents."""

    supported_suffixes: ClassVar[set[str]] = {".jsonl"}

    def can_handle(self, path: Path) -> bool:
        """Return whether path is a JSONL file."""
        return path.suffix.lower() in self.supported_suffixes

    def extract(self, path: Path) -> list[ExtractedDocument]:
        """Detect supported JSONL shape and extract conversations."""
        rows = _read_jsonl(path)
        if not rows:
            raise BrainError(f"{path}: JSONL file is empty")

        if all(_is_format_a(row) for row in rows):
            return [_render_conversation(row, path, index) for index, row in enumerate(rows, start=1)]

        if all(_is_format_b(row) for row in rows):
            grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
            for row in rows:
                grouped.setdefault(str(row["conversation_id"]), []).append(row)
            return [_render_message_group(conversation_id, messages, path) for conversation_id, messages in grouped.items()]

        raise BrainError(f"{path}: JSONL rows must be conversations with messages or messages with conversation_id")

    def estimate_tokens(self, path: Path) -> int:
        """Estimate JSONL tokens with a deterministic char-based heuristic."""
        return max(1, (len(path.read_text(encoding="utf-8")) + 3) // 4)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BrainError(f"{path}: invalid JSON on line {line_number}: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise BrainError(f"{path}: line {line_number} must be a JSON object")
        rows.append(parsed)
    return rows


def _is_format_a(row: dict[str, Any]) -> bool:
    return isinstance(row.get("messages"), list)


def _is_format_b(row: dict[str, Any]) -> bool:
    return "conversation_id" in row and "role" in row and "content" in row


def _render_conversation(row: dict[str, Any], path: Path, index: int) -> ExtractedDocument:
    messages = row.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BrainError(f"{path}: conversation row {index} missing non-empty messages")
    conversation_id = str(row.get("id") or row.get("conversation_id") or f"conversation-{index}")
    title = str(row.get("title") or f"Conversation {conversation_id}")
    has_model = "model" in row or any(isinstance(message, dict) and "model" in message for message in messages)
    return ExtractedDocument(
        title=title,
        content=_render_messages(messages, path),
        metadata={
            "original_path": str(path),
            "source_suffix": "jsonl",
            "conversation_id": conversation_id,
        },
        suggested_kind=EventKind.AI_CHAT if has_model else EventKind.HUMAN_CHAT,
    )


def _render_message_group(conversation_id: str, messages: list[dict[str, Any]], path: Path) -> ExtractedDocument:
    has_model = any("model" in message for message in messages)
    title = f"Conversation {conversation_id}"
    return ExtractedDocument(
        title=title,
        content=_render_messages(messages, path),
        metadata={
            "original_path": str(path),
            "source_suffix": "jsonl",
            "conversation_id": conversation_id,
        },
        suggested_kind=EventKind.AI_CHAT if has_model else EventKind.HUMAN_CHAT,
    )


def _render_messages(messages: list[Any], path: Path) -> str:
    rendered: list[str] = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            raise BrainError(f"{path}: message {index} must be a JSON object")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not role.strip():
            raise BrainError(f"{path}: message {index} missing role")
        if not isinstance(content, str):
            raise BrainError(f"{path}: message {index} missing content")
        rendered.append(f"**{role.strip()}**: {content.strip()}")
    return "\n\n".join(rendered)
