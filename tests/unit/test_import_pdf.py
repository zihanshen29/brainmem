from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from brain.exceptions import BrainError
from brain.import_.extractors.pdf import PdfExtractor


class _FakePage:
    def __init__(self, text: str | None) -> None:
        self.text = text

    def extract_text(self) -> str | None:
        return self.text


def test_pdf_extracts_one_doc_per_five_pages_with_page_ranges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"fake")
    _install_fake_pypdf(monkeypatch, [f"page {index}" for index in range(1, 13)])

    docs = PdfExtractor().extract(path)

    assert len(docs) == 3
    assert [doc.metadata["page_range"] for doc in docs] == [[1, 5], [6, 10], [11, 12]]
    assert docs[0].content == "page 1\n\npage 2\n\npage 3\n\npage 4\n\npage 5"
    assert docs[0].metadata["original_path"] == str(path)
    assert docs[0].metadata["source_suffix"] == "pdf"


def test_pdf_ten_pages_extracts_two_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "ten.pdf"
    path.write_bytes(b"fake")
    _install_fake_pypdf(monkeypatch, [f"page {index}" for index in range(1, 11)])

    docs = PdfExtractor().extract(path)

    assert len(docs) == 2
    assert [doc.metadata["page_range"] for doc in docs] == [[1, 5], [6, 10]]


def test_pdf_skips_empty_pages_but_preserves_chunk_page_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"fake")
    _install_fake_pypdf(monkeypatch, ["one", "", None, "four", "five", "", "seven"])

    docs = PdfExtractor().extract(path)

    assert len(docs) == 2
    assert docs[0].metadata["page_range"] == [1, 5]
    assert docs[0].content == "one\n\nfour\n\nfive"
    assert docs[1].metadata["page_range"] == [6, 7]
    assert docs[1].content == "seven"


def test_scanned_pdf_raises_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"fake")
    _install_fake_pypdf(monkeypatch, ["", None, "   "])

    with pytest.raises(BrainError, match="no text extracted \\(likely scanned\\)"):
        PdfExtractor().extract(path)


def _install_fake_pypdf(monkeypatch: pytest.MonkeyPatch, page_texts: list[str | None]) -> None:
    class FakeReader:
        def __init__(self, path: Path) -> None:
            del path
            self.pages = [_FakePage(text) for text in page_texts]

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=FakeReader))
