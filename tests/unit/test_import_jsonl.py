from __future__ import annotations

from pathlib import Path

import pytest

from brain.exceptions import BrainError
from brain.import_.extractors.jsonl import JsonlExtractor
from brain.models.event import EventKind


def test_jsonl_format_a_extracts_one_doc_per_conversation_and_detects_ai(tmp_path: Path) -> None:
    path = tmp_path / "conversations.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"id":"c1","title":"AI chat","model":"gpt","messages":[{"role":"user","content":"Hi"},{"role":"assistant","content":"Hello"}]}',
                '{"id":"c2","messages":[{"role":"user","content":"Human note"},{"role":"friend","content":"Reply"}]}',
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )

    docs = JsonlExtractor().extract(path)

    assert len(docs) == 2
    assert docs[0].title == "AI chat"
    assert docs[0].content == "**user**: Hi\n\n**assistant**: Hello"
    assert docs[0].metadata["conversation_id"] == "c1"
    assert docs[0].suggested_kind is EventKind.AI_CHAT
    assert docs[1].suggested_kind is EventKind.HUMAN_CHAT


def test_jsonl_format_b_groups_by_conversation_id_and_message_model(tmp_path: Path) -> None:
    path = tmp_path / "messages.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"conversation_id":"a","role":"user","content":"A1"}',
                '{"conversation_id":"b","role":"user","content":"B1","model":"claude"}',
                '{"conversation_id":"a","role":"assistant","content":"A2"}',
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )

    docs = JsonlExtractor().extract(path)

    assert [doc.metadata["conversation_id"] for doc in docs] == ["a", "b"]
    assert docs[0].content == "**user**: A1\n\n**assistant**: A2"
    assert docs[0].suggested_kind is EventKind.HUMAN_CHAT
    assert docs[1].suggested_kind is EventKind.AI_CHAT


def test_jsonl_bad_json_raises_for_importer_failure(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"conversation_id":"a"\n', encoding="utf-8")

    with pytest.raises(BrainError, match="invalid JSON"):
        JsonlExtractor().extract(path)


def test_jsonl_missing_core_fields_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad-shape.jsonl"
    path.write_text('{"conversation_id":"a","role":"user"}\n', encoding="utf-8")

    with pytest.raises(BrainError, match="must be conversations with messages"):
        JsonlExtractor().extract(path)
