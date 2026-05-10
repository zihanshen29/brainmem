from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.cli.init import init_brain
from brain.exceptions import BrainError
from brain.models import Frontmatter, Page, PageType, Tier
from brain.pages import write_page
from brain.pipeline.injection import estimate_tokens, inject


@pytest.fixture()
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    _seed_pages(root)
    return root


def test_inject_markdown_includes_budget_sources_and_fragments(brain_root: Path) -> None:
    result = inject(brain_root, "memory injection Alice", budget=300, mode="keyword-only", top=2)

    assert result.query == "memory injection Alice"
    assert result.budget == 300
    assert result.used_tokens <= 300
    assert result.used_tokens == estimate_tokens(result.content)
    assert result.fragment_count == 2
    assert [fragment.slug for fragment in result.fragments] == ["alice", "brain-injection"]
    assert result.fragments[0].relative_path == "pages/entities/alice.md"
    assert "# BrainMem Injection" in result.content
    assert "Budget used:" in result.content
    assert "Fragment count: 2" in result.content
    assert "alice (pages/entities/alice.md)" in result.content
    assert "### Alice (`alice`)" in result.content
    assert "Alice owns the memory injection design." in result.content


def test_inject_small_budget_truncates_and_skips(brain_root: Path) -> None:
    result = inject(brain_root, "memory injection Alice", budget=42, mode="keyword-only", top=2)

    assert result.used_tokens <= 42
    assert result.used_tokens == estimate_tokens(result.content)
    assert result.fragment_count == 1
    assert result.fragments[0].truncated is True
    assert result.skipped[0].slug == "brain-injection"
    assert "Truncated: yes" in result.content
    assert "Skipped: brain-injection" in result.content
    assert any("truncated" in warning for warning in result.warnings)
    assert any("skipped" in warning for warning in result.warnings)


def test_inject_tiny_budget_uses_final_content_budget(brain_root: Path) -> None:
    result = inject(brain_root, "memory injection Alice", budget=1, mode="keyword-only", top=2)

    assert result.used_tokens == estimate_tokens(result.content)
    assert result.used_tokens <= 1
    assert result.content == ".\n"


def test_inject_text_output(brain_root: Path) -> None:
    result = inject(
        brain_root,
        "memory injection Alice",
        budget=300,
        output_format="text",
        mode="keyword-only",
        top=1,
    )

    assert result.output_format == "text"
    assert result.fragment_count == 1
    assert result.content.startswith("BrainMem Injection\n")
    assert "# BrainMem Injection" not in result.content
    assert "Alice [alice]" in result.content
    assert "Source: pages/entities/alice.md" in result.content


def test_inject_includes_snapshot_before_retrieved_pages(brain_root: Path) -> None:
    _write_snapshot(brain_root, "Snapshot context should appear before retrieved pages.")

    result = inject(brain_root, "memory injection Alice", budget=500, mode="keyword-only", top=2)

    assert result.used_tokens <= 500
    assert result.used_tokens == estimate_tokens(result.content)
    assert [fragment.slug for fragment in result.fragments] == [
        "snapshot",
        "alice",
        "brain-injection",
    ]
    assert result.fragments[0].relative_path == "scratch/SNAPSHOT.md"
    assert result.content.index("### Current Snapshot (`snapshot`)") < result.content.index(
        "### Alice (`alice`)"
    )


def test_inject_no_snapshot_restores_retrieved_page_order(brain_root: Path) -> None:
    _write_snapshot(brain_root, "Snapshot context should be ignored.")

    result = inject(
        brain_root,
        "memory injection Alice",
        budget=300,
        mode="keyword-only",
        top=2,
        include_snapshot=False,
    )

    assert result.used_tokens <= 300
    assert result.used_tokens == estimate_tokens(result.content)
    assert [fragment.slug for fragment in result.fragments] == ["alice", "brain-injection"]
    assert "snapshot" not in result.content
    assert "Snapshot context should be ignored." not in result.content


def test_inject_snapshot_participates_in_small_budget_first(brain_root: Path) -> None:
    _write_snapshot(
        brain_root,
        "Snapshot context has priority in the budget. " * 20,
    )

    result = inject(brain_root, "memory injection Alice", budget=70, mode="keyword-only", top=2)

    assert result.used_tokens <= 70
    assert result.used_tokens == estimate_tokens(result.content)
    assert result.fragments[0].slug == "snapshot"
    assert result.fragments[0].truncated is True
    assert all(fragment.slug != "snapshot" for fragment in result.skipped)
    assert "Truncated: yes" in result.content


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": "   "}, "query must not be empty"),
        ({"query": "Alice", "budget": 0}, "budget must be positive"),
        ({"query": "Alice", "top": 0}, "top must be positive"),
        ({"query": "Alice", "output_format": "json"}, "unsupported injection format"),
        ({"query": "Alice", "mode": "provider-mode"}, "unsupported ask mode"),
    ],
)
def test_inject_rejects_invalid_parameters(
    brain_root: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(BrainError, match=message):
        inject(brain_root, **kwargs)  # type: ignore[arg-type]


def _seed_pages(root: Path) -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    write_page(
        root / "pages" / "entities" / "alice.md",
        Page(
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
            compiled_truth=(
                "Alice owns the memory injection design. "
                "Token-aware injection should preserve source paths and avoid provider calls."
            ),
            timeline=[
                "- 2026-04-27 [event:01KQA8R9KVCG906A0203VYEQF7]: Alice drafted injection notes.",
                "- 2026-04-28 [event:01KQA8VZMXBAV7AKF5JFB4KQ9C]: Alice validated token budgeting.",
            ],
            sources=["events.jsonl"],
        ),
    )
    write_page(
        root / "pages" / "projects" / "brain-injection.md",
        Page(
            frontmatter=Frontmatter(
                type=PageType.PROJECT,
                slug="brain-injection",
                title="Brain Injection",
                created=now,
                updated=now,
                tags=[],
                aliases=[],
                external_ids={},
            ),
            compiled_truth=(
                "Brain injection assembles memory fragments for prompts. "
                "The implementation uses deterministic budget estimation."
            ),
            timeline=[
                "- 2026-04-26 [event:01KQA8R9KVCG906A0203VYEQF7]: Brain injection scope was defined."
            ],
            sources=["docs/cli.md"],
        ),
    )


def _write_snapshot(root: Path, content: str) -> None:
    snapshot = root / "scratch" / "SNAPSHOT.md"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(content, encoding="utf-8")
