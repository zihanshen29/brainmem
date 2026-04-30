from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from brain.import_.extractors.base import ExtractedDocument
from brain.models.event import EventKind

MAX_SINGLE_DOC_CHARS = 8000
HEADING_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<body>.*?)(?:\r?\n)---(?:\r?\n|\Z)", re.DOTALL)


class MarkdownExtractor:
    """Extract markdown and plain-text files into import documents."""

    supported_suffixes: ClassVar[set[str]] = {".md", ".txt"}

    def can_handle(self, path: Path) -> bool:
        """Return whether path is a markdown or text file."""
        return path.suffix.lower() in self.supported_suffixes

    def extract(self, path: Path) -> list[ExtractedDocument]:
        """Extract a short file as one doc, or split long markdown by H1 headings."""
        text = path.read_text(encoding="utf-8")
        is_markdown = path.suffix.lower() == ".md"
        frontmatter, content = _split_frontmatter(text) if is_markdown else ({}, text)
        metadata = _base_metadata(path, frontmatter)

        if not is_markdown or len(content) <= MAX_SINGLE_DOC_CHARS:
            return [
                ExtractedDocument(
                    title=path.stem,
                    content=content,
                    metadata=metadata,
                    suggested_kind=EventKind.RAW_IMPORTED,
                )
            ]

        docs = _split_markdown_by_heading(content, path, metadata)
        if docs:
            return docs
        return [
            ExtractedDocument(
                title=path.stem,
                content=content,
                metadata=metadata,
                suggested_kind=EventKind.RAW_IMPORTED,
            )
        ]

    def estimate_tokens(self, path: Path) -> int:
        """Estimate tokens with a deterministic char-based heuristic."""
        return max(1, (len(path.read_text(encoding="utf-8")) + 3) // 4)


def _split_markdown_by_heading(
    text: str,
    path: Path,
    metadata: dict[str, object],
) -> list[ExtractedDocument]:
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return []

    prefix = text[: matches[0].start()]
    docs: list[ExtractedDocument] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = (prefix if index == 0 else "") + text[match.start() : end].strip()
        if not content.strip():
            continue
        section_metadata = {
            **metadata,
            "section_index": index + 1,
            "section_heading": match.group(1).strip(),
        }
        docs.append(
            ExtractedDocument(
                title=f"{path.stem} - {match.group(1).strip()}",
                content=content,
                metadata=section_metadata,
                suggested_kind=EventKind.RAW_IMPORTED,
            )
        )
    return docs


def _base_metadata(path: Path, frontmatter: dict[str, str]) -> dict[str, object]:
    metadata: dict[str, object] = {
        "original_path": str(path),
        "source_suffix": path.suffix.lower().lstrip("."),
    }
    if frontmatter:
        metadata["frontmatter"] = frontmatter
        metadata["frontmatter_status"] = "preserved"
    else:
        metadata["frontmatter_status"] = "generated"
        metadata["title"] = path.stem
    return metadata


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text

    parsed: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", maxsplit=1)
        parsed[key.strip()] = value.strip().strip('"')
    return parsed, text[match.end() :]
