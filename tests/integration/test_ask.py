from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli.init import init_brain
from brain.cli.main import app
from brain.exceptions import LLMError
from brain.llm import client as llm_client
from brain.models import Frontmatter, Page, PageType, Tier
from brain.pages import write_page

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


@pytest.fixture()
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    _write_ask_pages(root)
    return root


def test_cli_ask_default_output_lists_ranked_pages(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["ask", "Alice Brain"])

    assert result.exit_code == 0
    assert "1. [entity] alice - Alice" in result.stdout
    assert "Compiled truth: Alice maintains the Brain memory system." in result.stdout
    assert "Recent: 2026-04-28: Alice started the ask CLI task." in result.stdout
    assert "Score: " in result.stdout


def test_cli_ask_type_filters_results(brain_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["ask", "Brain", "--type", "project"])

    assert result.exit_code == 0
    assert "[project] brain-ask - Brain Ask CLI" in result.stdout
    assert "[entity] alice - Alice" not in result.stdout


def test_cli_ask_top_limits_results(brain_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["ask", "Brain", "--top", "1"])

    assert result.exit_code == 0
    assert result.stdout.count("Compiled truth:") == 1


def test_cli_ask_sql_outputs_trace_and_results(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["ask", "Brain", "--sql", "--top", "1"])

    assert result.exit_code == 0
    assert "SQL trace:" in result.stdout
    assert "FROM entities" in result.stdout
    assert "Params: ['Brain', 'Brain']" in result.stdout
    assert "1. [" in result.stdout


def test_cli_ask_explain_outputs_mocked_answer(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[dict[str, object]]]] = []

    def fake_answer(query: str, pages: list[dict[str, object]]) -> llm_client.QuestionAnswer:
        calls.append((query, pages))
        return llm_client.QuestionAnswer(
            answer="Alice is working on the Brain ask CLI.",
            sources=["alice", "brain-ask"],
        )

    monkeypatch.setattr(llm_client, "answer_question", fake_answer)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["ask", "What is Alice doing?", "--explain"])

    assert result.exit_code == 0
    assert "1. [entity] alice - Alice" in result.stdout
    assert "Answer:" in result.stdout
    assert "Alice is working on the Brain ask CLI." in result.stdout
    assert "Sources:" in result.stdout
    assert "- alice" in result.stdout
    assert calls[0][0] == "What is Alice doing?"
    assert calls[0][1][0]["slug"] == "alice"


def test_cli_ask_explain_failure_exits_with_error(
    brain_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_answer(query: str, pages: list[dict[str, object]]) -> llm_client.QuestionAnswer:
        raise LLMError("LLM unavailable")

    monkeypatch.setattr(llm_client, "answer_question", fail_answer)
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["ask", "What is Alice doing?", "--explain"])

    assert result.exit_code == 1
    assert "Error: LLM unavailable" in result.stderr


def _write_ask_pages(root: Path) -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    _write_page(
        root,
        dirname="entities",
        page=Page(
            frontmatter=Frontmatter(
                type=PageType.ENTITY,
                slug="alice",
                title="Alice",
                tier=Tier.TIER_2,
                created=now,
                updated=now,
                tags=[],
                aliases=[],
                external_ids={},
            ),
            compiled_truth="Alice maintains the Brain memory system.",
            timeline=[
                "- 2026-04-27 [event:01KQA8R9KVCG906A0203VYEQF7]: Alice planned CLI work.",
                "- 2026-04-28 [event:01KQA8VZMXBAV7AKF5JFB4KQ9C]: Alice started the ask CLI task.",
            ],
            sources=["events.jsonl"],
        ),
    )
    _write_page(
        root,
        dirname="projects",
        page=Page(
            frontmatter=Frontmatter(
                type=PageType.PROJECT,
                slug="brain-ask",
                title="Brain Ask CLI",
                created=now,
                updated=now,
                tags=[],
                aliases=[],
                external_ids={},
            ),
            compiled_truth="Brain ask provides lexical page search and optional explanations.",
            timeline=[
                "- 2026-04-26 [event:01KQA8R9KVCG906A0203VYEQF7]: Brain ask scope was defined."
            ],
            sources=["files/cli.md"],
        ),
    )


def _write_page(root: Path, *, dirname: str, page: Page) -> None:
    write_page(root / "pages" / dirname / f"{page.frontmatter.slug}.md", page)
