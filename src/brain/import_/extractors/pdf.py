from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from brain.exceptions import BrainError
from brain.import_.extractors.base import ExtractedDocument
from brain.models.event import EventKind

PAGES_PER_DOC = 5


class PdfExtractor:
    """Extract text from PDF files into page-range markdown documents."""

    supported_suffixes: ClassVar[set[str]] = {".pdf"}

    def can_handle(self, path: Path) -> bool:
        """Return whether path is a PDF file."""
        return path.suffix.lower() in self.supported_suffixes

    def extract(self, path: Path) -> list[ExtractedDocument]:
        """Extract PDF text in fixed page groups, skipping empty pages."""
        from pypdf import PdfReader

        reader = PdfReader(path)
        docs: list[ExtractedDocument] = []
        extracted_any_text = False
        for chunk_start in range(0, len(reader.pages), PAGES_PER_DOC):
            chunk_pages = reader.pages[chunk_start : chunk_start + PAGES_PER_DOC]
            start_page = chunk_start + 1
            end_page = chunk_start + len(chunk_pages)
            texts = []
            for page in chunk_pages:
                text = page.extract_text() or ""
                if text.strip():
                    texts.append(text.strip())
            if not texts:
                continue
            extracted_any_text = True
            docs.append(
                ExtractedDocument(
                    title=f"{path.stem} pp.{start_page}-{end_page}",
                    content="\n\n".join(texts),
                    metadata={
                        "original_path": str(path),
                        "source_suffix": "pdf",
                        "page_range": [start_page, end_page],
                    },
                    suggested_kind=EventKind.RAW_IMPORTED,
                )
            )
        if not extracted_any_text:
            raise BrainError(f"{path}: no text extracted (likely scanned)")
        return docs

    def estimate_tokens(self, path: Path) -> int:
        """Estimate PDF tokens from file size without parsing the PDF."""
        return max(1, path.stat().st_size // 4)
