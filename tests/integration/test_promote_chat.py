from __future__ import annotations

import importlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from brain.cli.init import init_brain
from brain.cli.main import app
from brain.exceptions import BrainError
from brain.ledger import append_event, read_all
from brain.llm import client as llm_client
from brain.llm.client import PromotedChatDraft
from brain.models import Event, EventKind, Frontmatter, Page, PageType
from brain.pages import TimelineEntry, format_entry, parse_page, write_page
from brain.pipeline import signal_detect
from brain.pipeline.signal_detect import SignalExtraction

VALID_ULID = "01KQA8R9KVCG906A0203VYEQF7"
SECOND_ULID = "01KQA8VZMXBAV7AKF5JFB4KQ9C"
THIRD_ULID = "01KQA8XSH0AW2F2C5DB2N4MK9J"

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


@pytest.fixture()
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    return root


def test_promote_chat_pipeline_creates_conversation_page_event_ingest_and_commit(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _append_chat(brain_root, VALID_ULID, "User: Keep this architecture note.\nAssistant: Done.")
    _install_promote_draft(monkeypatch)
    _install_empty_signal_detector(monkeypatch)

    from brain.pipeline.promote_chat import promote_chat

    report = promote_chat(brain_root, VALID_ULID)

    assert report.source_event_id == VALID_ULID
    assert report.page_slug == "architecture-note"
    assert report.page_path == "pages/conversations/2026-04-28_architecture-note.md"
    assert report.page_event_id
    assert report.ingest_report.processed == 1
    assert report.ingest_report.facts_added == 0
    assert report.ingest_report.review_items_created == 0
    assert report.committed is True

    page = parse_page(brain_root / report.page_path)
    assert page.frontmatter.type is PageType.CONVERSATION
    assert page.frontmatter.slug == "architecture-note"
    assert page.frontmatter.external_ids == {"source_event": VALID_ULID}
    assert page.compiled_truth == "The chat captured a stable architecture note."
    assert any(VALID_ULID in entry for entry in page.timeline)
    assert any("Captured an architecture note." in entry for entry in page.timeline)

    page_events = [
        event
        for event in read_all(brain_root / "events.jsonl")
        if event.kind is EventKind.PAGE_EDITED
    ]
    assert len(page_events) == 1
    assert page_events[0].id == report.page_event_id
    assert page_events[0].metadata["source_event"] == VALID_ULID
    assert page_events[0].affected_pages == ["architecture-note"]

    assert _git_output(brain_root, "log", "-1", "--pretty=%s") == (
        f"promote-chat: {VALID_ULID} -> architecture-note\n"
    )


def test_promote_chat_duplicate_rejects_without_new_page_event(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _append_chat(brain_root, VALID_ULID, "User: Keep this.\nAssistant: Done.")
    _install_promote_draft(monkeypatch)
    _install_empty_signal_detector(monkeypatch)

    from brain.pipeline.promote_chat import promote_chat

    promote_chat(brain_root, VALID_ULID)
    before_page_events = _page_edited_count(brain_root)

    with pytest.raises(BrainError, match="already promoted"):
        promote_chat(brain_root, VALID_ULID)

    assert _page_edited_count(brain_root) == before_page_events


def test_promote_chat_duplicate_rejects_existing_non_conversation_page(
    brain_root: Path,
) -> None:
    _append_chat(brain_root, VALID_ULID, "User: Keep this.\nAssistant: Done.")
    _write_existing_project(brain_root, source_event_id=VALID_ULID)
    before_page_events = _page_edited_count(brain_root)

    from brain.pipeline.promote_chat import promote_chat

    with pytest.raises(BrainError, match="already promoted"):
        promote_chat(brain_root, VALID_ULID)

    assert _page_edited_count(brain_root) == before_page_events


def test_cli_promote_chat_accepts_prefix_title_and_slug_overrides(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _append_chat(brain_root, VALID_ULID, "User: CLI promotion.\nAssistant: Ready.")
    _install_promote_draft(monkeypatch, title="Ignored Draft Title")
    _install_empty_signal_detector(monkeypatch)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(
        app,
        [
            "promote-chat",
            VALID_ULID[:10],
            "--title",
            "Custom Conversation Title",
            "--slug",
            "custom-chat",
        ],
    )

    assert result.exit_code == 0
    assert "Promote chat summary:" in result.stdout
    assert "slug=custom-chat" in result.stdout
    assert "path=pages/conversations/2026-04-28_custom-chat.md" in result.stdout
    assert f"source_event_id={VALID_ULID}" in result.stdout
    assert "page_event_id=" in result.stdout
    assert "ingest_processed=1" in result.stdout
    assert "ingest_facts_added=0" in result.stdout
    assert "ingest_review_items_created=0" in result.stdout
    assert "committed=true" in result.stdout

    page = parse_page(brain_root / "pages/conversations/2026-04-28_custom-chat.md")
    assert page.frontmatter.title == "Custom Conversation Title"
    assert page.frontmatter.slug == "custom-chat"


def test_promote_chat_rejects_non_ai_chat_event(
    brain_root: Path,
) -> None:
    append_event(
        brain_root / "events.jsonl",
        Event(
            id=VALID_ULID,
            timestamp=_timestamp(),
            kind=EventKind.RAW_IMPORTED,
            source_ref="raw/import.md",
            raw_payload="Not an AI chat.",
        ),
    )

    from brain.pipeline.promote_chat import promote_chat

    with pytest.raises(BrainError, match="not an ai_chat"):
        promote_chat(brain_root, VALID_ULID)


def test_promote_chat_rejects_ambiguous_event_prefix(
    brain_root: Path,
) -> None:
    _append_chat(brain_root, VALID_ULID, "First chat.")
    _append_chat(brain_root, SECOND_ULID, "Second chat.")

    from brain.pipeline.promote_chat import promote_chat

    with pytest.raises(BrainError, match="ambiguous"):
        promote_chat(brain_root, "01KQA8")


def test_promote_chat_rejects_invalid_slug(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _append_chat(brain_root, VALID_ULID, "Chat text.")
    _install_promote_draft(monkeypatch)

    from brain.pipeline.promote_chat import promote_chat

    with pytest.raises(BrainError, match="Invalid conversation slug"):
        promote_chat(brain_root, VALID_ULID, slug="Invalid Slug")


def test_promote_chat_rejects_path_collision(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _append_chat(brain_root, VALID_ULID, "Chat text.")
    _install_promote_draft(monkeypatch, title="Existing")
    _write_existing_conversation(brain_root, "existing")

    from brain.pipeline.promote_chat import promote_chat

    with pytest.raises(BrainError, match="already exists"):
        promote_chat(brain_root, VALID_ULID, slug="existing")


def test_cli_promote_chat_brain_error_outputs_stderr_and_exit_one(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain.cli import promote_chat as promote_chat_cli

    def raise_brain_error(*_args: object, **_kwargs: object) -> None:
        raise BrainError("promotion failed")

    monkeypatch.setattr(promote_chat_cli, "_run_promote_chat", raise_brain_error)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["promote-chat", VALID_ULID])

    assert result.exit_code == 1
    assert "Error: promotion failed" in result.stderr


def _append_chat(root: Path, event_id: str, text: str) -> None:
    append_event(
        root / "events.jsonl",
        Event(
            id=event_id,
            timestamp=_timestamp(),
            kind=EventKind.AI_CHAT,
            source_ref=f"ai-chat/{event_id}.md",
            raw_payload=text,
        ),
    )


def _timestamp() -> datetime:
    return datetime(2026, 4, 28, 12, 0, tzinfo=UTC)


def _install_promote_draft(
    monkeypatch: pytest.MonkeyPatch,
    *,
    title: str = "Architecture Note",
) -> None:
    def fake_promote_chat(
        raw_text: str,
        title_hint: str | None = None,
        slug_hint: str | None = None,
    ) -> PromotedChatDraft:
        assert raw_text
        return PromotedChatDraft(
            title=title,
            compiled_truth="The chat captured a stable architecture note.",
            timeline_description="Captured an architecture note.",
        )

    monkeypatch.setattr(llm_client, "promote_chat", fake_promote_chat)


def _install_empty_signal_detector(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any] | None]:
    calls: list[dict[str, Any] | None] = []

    def fake_detect_signal(text: str, hint: dict[str, Any] | None = None) -> SignalExtraction:
        calls.append(hint)
        return SignalExtraction(
            entities=[],
            facts=[],
            timeline_summary="No durable entity facts extracted.",
            suggested_page_type=PageType.CONVERSATION,
        )

    def fake_extract_signal(text: str) -> dict[str, Any]:
        return fake_detect_signal(text).model_dump(mode="json")

    monkeypatch.setattr(signal_detect, "detect_signal", fake_detect_signal)
    monkeypatch.setattr(llm_client, "extract_signal", fake_extract_signal)

    ingest_module = importlib.import_module("brain.pipeline.ingest")

    monkeypatch.setattr(ingest_module, "detect_signal", fake_detect_signal)
    return calls


def _write_existing_conversation(root: Path, slug: str) -> None:
    path = root / "pages" / "conversations" / f"2026-04-28_{slug}.md"
    page = Page(
        frontmatter=Frontmatter(
            type=PageType.CONVERSATION,
            slug=slug,
            title="Existing",
            created=_timestamp(),
            updated=_timestamp(),
            external_ids={"source_event": THIRD_ULID},
        ),
        compiled_truth="Existing conversation.",
        timeline=[
            format_entry(
                TimelineEntry(
                    date="2026-04-28",
                    event_id=THIRD_ULID,
                    description="Existing conversation.",
                )
            )
        ],
        sources=[f"events.jsonl:{THIRD_ULID}"],
    )
    write_page(path, page)


def _write_existing_project(root: Path, *, source_event_id: str) -> None:
    path = root / "pages" / "projects" / "existing-project.md"
    page = Page(
        frontmatter=Frontmatter(
            type=PageType.PROJECT,
            slug="existing-project",
            title="Existing Project",
            created=_timestamp(),
            updated=_timestamp(),
            external_ids={"source_event": source_event_id},
        ),
        compiled_truth="Existing project page.",
        timeline=[
            format_entry(
                TimelineEntry(
                    date="2026-04-28",
                    event_id=source_event_id,
                    description="Existing project page.",
                )
            )
        ],
        sources=[f"events.jsonl:{source_event_id}"],
    )
    write_page(path, page)


def _page_edited_count(root: Path) -> int:
    return sum(
        1 for event in read_all(root / "events.jsonl") if event.kind is EventKind.PAGE_EDITED
    )


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        encoding="utf-8",
        env=_git_env(),
        check=True,
    )
    return result.stdout


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env
