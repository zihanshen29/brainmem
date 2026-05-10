from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.exceptions import BrainError
from brain.paths import BrainPaths
from brain.pipeline.scratch import append_working, rebuild_snapshot


def test_brain_paths_include_scratch_paths(tmp_path: Path) -> None:
    paths = BrainPaths(tmp_path)

    assert paths.scratch_dir == tmp_path / "scratch"
    assert paths.working_buffer == tmp_path / "scratch" / "working.md"
    assert paths.snapshot_path == tmp_path / "scratch" / "SNAPSHOT.md"


def test_append_working_rejects_empty_content(tmp_path: Path) -> None:
    with pytest.raises(BrainError, match="Scratch content is empty"):
        append_working(tmp_path, " \n\t ")


def test_append_working_creates_directory_and_file(tmp_path: Path) -> None:
    report = append_working(
        tmp_path,
        "first note",
        source="manual",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )

    path = tmp_path / report.path
    assert path == tmp_path / "scratch" / "working.md"
    assert path.exists()
    assert report.entries == 1
    assert report.char_count == len("first note")
    assert report.created is True
    assert path.read_text(encoding="utf-8") == (
        "\n## 2026-05-10T12:00:00Z - source: manual\n\nfirst note\n"
    )


def test_append_working_is_append_only_and_preserves_order(tmp_path: Path) -> None:
    append_working(tmp_path, "first note", source="manual", timestamp="2026-05-10T12:00:00Z")
    first_content = (tmp_path / "scratch" / "working.md").read_text(encoding="utf-8")

    report = append_working(tmp_path, "second note", source="agent", timestamp="2026-05-10T12:01:00Z")

    content = (tmp_path / "scratch" / "working.md").read_text(encoding="utf-8")
    assert report.created is False
    assert content.startswith(first_content)
    assert content == (
        "\n## 2026-05-10T12:00:00Z - source: manual\n\nfirst note\n"
        "\n## 2026-05-10T12:01:00Z - source: agent\n\nsecond note\n"
    )


def test_rebuild_snapshot_requires_working_buffer(tmp_path: Path) -> None:
    with pytest.raises(BrainError, match="Working scratch buffer not found"):
        rebuild_snapshot(tmp_path, timestamp="2026-05-10T12:00:00Z")


def test_rebuild_snapshot_uses_recent_items(tmp_path: Path) -> None:
    append_working(tmp_path, "one", timestamp="2026-05-10T12:00:00Z")
    append_working(tmp_path, "two", timestamp="2026-05-10T12:01:00Z")
    append_working(tmp_path, "three", timestamp="2026-05-10T12:02:00Z")

    report = rebuild_snapshot(tmp_path, max_items=2, timestamp="2026-05-10T12:03:00Z")

    snapshot = (tmp_path / report.path).read_text(encoding="utf-8")
    assert report.path == "scratch/SNAPSHOT.md"
    assert report.entries == 2
    assert report.created is True
    assert report.updated is True
    assert "Generated: 2026-05-10T12:03:00Z" in snapshot
    assert "one" not in snapshot
    assert "two" in snapshot
    assert "three" in snapshot
    assert snapshot.index("two") < snapshot.index("three")


def test_rebuild_snapshot_respects_max_chars(tmp_path: Path) -> None:
    append_working(tmp_path, "12345", timestamp="2026-05-10T12:00:00Z")
    append_working(tmp_path, "abcdef", timestamp="2026-05-10T12:01:00Z")
    append_working(tmp_path, "xyz", timestamp="2026-05-10T12:02:00Z")

    report = rebuild_snapshot(tmp_path, max_items=10, max_chars=9, timestamp="2026-05-10T12:03:00Z")

    snapshot = (tmp_path / "scratch" / "SNAPSHOT.md").read_text(encoding="utf-8")
    assert report.entries == 2
    assert report.char_count == len("abcdef") + len("xyz")
    assert "12345" not in snapshot
    assert "abcdef" in snapshot
    assert "xyz" in snapshot


def test_rebuild_snapshot_does_not_import_provider_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    append_working(tmp_path, "local only", timestamp="2026-05-10T12:00:00Z")

    real_import = __import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith(("brain.llm", "brain.embedding")):
            raise AssertionError(f"provider module imported: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked_import)

    rebuild_snapshot(tmp_path, timestamp="2026-05-10T12:01:00Z")
