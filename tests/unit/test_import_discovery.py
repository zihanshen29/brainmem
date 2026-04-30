from pathlib import Path

from brain.import_.cost import cost_estimate
from brain.import_.discovery import discover_files


def test_discover_files_recurses_skips_unsupported_and_hashes_stably(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "a.md").write_text("# A\n", encoding="utf-8")
    (nested / "b.txt").write_text("B\n", encoding="utf-8")
    (nested / "c.pdf").write_bytes(b"%PDF text")
    (nested / "d.jsonl").write_text('{"messages":[]}\n', encoding="utf-8")
    (nested / "ignored.png").write_bytes(b"png")

    first = discover_files(root)
    second = discover_files(root)

    assert [file.relative_path for file in first] == ["a.md", "nested/b.txt", "nested/c.pdf", "nested/d.jsonl"]
    assert [file.kind for file in first] == ["md", "txt", "pdf", "jsonl"]
    assert [file.file_hash for file in first] == [file.file_hash for file in second]
    assert all(len(file.file_hash) == 64 for file in first)


def test_discover_files_kind_filter(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("B\n", encoding="utf-8")
    (tmp_path / "c.pdf").write_bytes(b"%PDF text")
    (tmp_path / "d.jsonl").write_text('{"messages":[]}\n', encoding="utf-8")

    files = discover_files(tmp_path, kinds={"txt", "pdf"})

    assert [file.relative_path for file in files] == ["b.txt", "c.pdf"]
    assert [file.kind for file in files] == ["txt", "pdf"]


def test_discover_single_file_uses_file_name_as_relative_path(tmp_path: Path) -> None:
    path = tmp_path / "single.md"
    path.write_text("# Single\n", encoding="utf-8")

    files = discover_files(path)

    assert len(files) == 1
    assert files[0].path == path
    assert files[0].relative_path == "single.md"


def test_cost_estimate_counts_files_and_kinds(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("B\n", encoding="utf-8")
    files = discover_files(tmp_path)

    estimate = cost_estimate(files)

    assert estimate.total_files == 2
    assert estimate.by_kind == {"md": 1, "txt": 1}
    assert estimate.estimated_extraction_tokens > 0
    assert estimate.estimated_embedding_tokens == 2000
    assert estimate.estimated_total_usd > 0
