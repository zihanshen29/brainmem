from pathlib import Path

from brain.import_.extractors.markdown import MarkdownExtractor
from brain.models.event import EventKind


def test_markdown_short_file_extracts_one_doc_with_generated_metadata(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("# Note\nSmall body\n", encoding="utf-8")
    extractor = MarkdownExtractor()

    docs = extractor.extract(path)

    assert len(docs) == 1
    assert docs[0].title == "note"
    assert docs[0].content == "# Note\nSmall body\n"
    assert docs[0].metadata["frontmatter_status"] == "generated"
    assert docs[0].metadata["title"] == "note"
    assert docs[0].suggested_kind == EventKind.RAW_IMPORTED


def test_markdown_long_file_splits_by_h1_heading(tmp_path: Path) -> None:
    path = tmp_path / "long.md"
    long_intro = "intro\n" + ("x" * 8100)
    path.write_text(f"{long_intro}\n# First\nA\n# Second\nB\n", encoding="utf-8")
    extractor = MarkdownExtractor()

    docs = extractor.extract(path)

    assert [doc.title for doc in docs] == ["long - First", "long - Second"]
    assert docs[0].content.startswith("intro\n")
    assert "# First\nA" in docs[0].content
    assert docs[0].metadata["section_index"] == 1
    assert docs[0].metadata["section_heading"] == "First"
    assert docs[1].content == "# Second\nB"


def test_text_file_extracts_one_doc_even_when_long(tmp_path: Path) -> None:
    path = tmp_path / "plain.txt"
    path.write_text("plain\n" + ("x" * 9000), encoding="utf-8")
    extractor = MarkdownExtractor()

    docs = extractor.extract(path)

    assert len(docs) == 1
    assert docs[0].title == "plain"
    assert docs[0].metadata["source_suffix"] == "txt"


def test_frontmatter_is_preserved_in_metadata_not_content(tmp_path: Path) -> None:
    path = tmp_path / "fm.md"
    path.write_text("---\ntitle: Front Title\nsource: test\n---\n# Body\n", encoding="utf-8")
    extractor = MarkdownExtractor()

    docs = extractor.extract(path)

    assert len(docs) == 1
    assert not docs[0].content.startswith("---")
    assert docs[0].content == "# Body\n"
    assert docs[0].metadata["frontmatter_status"] == "preserved"
    assert docs[0].metadata["frontmatter"] == {"title": "Front Title", "source": "test"}


def test_text_file_does_not_parse_frontmatter_fence(tmp_path: Path) -> None:
    path = tmp_path / "plain.txt"
    path.write_text("---\ntitle: Not Metadata\n---\nBody\n", encoding="utf-8")
    extractor = MarkdownExtractor()

    docs = extractor.extract(path)

    assert len(docs) == 1
    assert docs[0].content.startswith("---\ntitle: Not Metadata")
    assert docs[0].metadata["frontmatter_status"] == "generated"


def test_long_markdown_splits_after_frontmatter_is_removed(tmp_path: Path) -> None:
    path = tmp_path / "long-fm.md"
    path.write_text(
        "---\ntitle: Long\n---\nintro\n" + ("x" * 8100) + "\n# First\nA\n# Second\nB\n",
        encoding="utf-8",
    )
    extractor = MarkdownExtractor()

    docs = extractor.extract(path)

    assert [doc.title for doc in docs] == ["long-fm - First", "long-fm - Second"]
    assert not docs[0].content.startswith("---")
    assert docs[0].content.startswith("intro\n")
    assert docs[0].metadata["frontmatter"] == {"title": "Long"}


def test_can_handle_and_estimate_tokens(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("12345", encoding="utf-8")
    extractor = MarkdownExtractor()

    assert extractor.can_handle(path)
    assert extractor.can_handle(tmp_path / "note.txt")
    assert not extractor.can_handle(tmp_path / "note.pdf")
    assert extractor.estimate_tokens(path) == 2
