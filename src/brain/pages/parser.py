from pathlib import Path
from typing import Any

import frontmatter
from pydantic import ValidationError

from brain.exceptions import PageParseError
from brain.models import Frontmatter, Page
from brain.pages.timeline import parse_entry

SECTION_DELIMITER = "---"
COMPILED_TRUTH_HEADING = "# Compiled truth"
TIMELINE_HEADING = "# Timeline"
SOURCES_HEADING = "# Sources"


def parse_page(path: Path) -> Page:
    """Parse a markdown page with YAML frontmatter and canonical body sections."""
    try:
        post = frontmatter.load(path, encoding="utf-8")
    except Exception as exc:  # pragma: no cover - frontmatter exposes several parser errors
        raise PageParseError(f"Could not read page {path}: {exc}") from exc

    try:
        front = Frontmatter.model_validate(dict(post.metadata))
        sections = _parse_body_sections(post.content, path)
        return Page(frontmatter=front, **sections)
    except PageParseError:
        raise
    except ValidationError as exc:
        raise PageParseError(f"Invalid page frontmatter in {path}: {exc}") from exc


def _parse_body_sections(content: str, path: Path | None = None) -> dict[str, Any]:
    groups = _split_section_groups(content)
    if len(groups) not in {2, 3}:
        raise PageParseError(_message(path, "Page body must contain Compiled truth and Timeline sections"))

    compiled_lines = _section_body(groups[0], COMPILED_TRUTH_HEADING, path)
    timeline_lines = _section_body(groups[1], TIMELINE_HEADING, path)
    source_lines = (
        _section_body(groups[2], SOURCES_HEADING, path) if len(groups) == 3 else []
    )

    timeline = [line for line in timeline_lines if line.strip()]
    for line in timeline:
        parse_entry(line)

    return {
        "compiled_truth": "\n".join(compiled_lines).strip(),
        "timeline": timeline,
        "sources": _parse_sources(source_lines),
    }


def _split_section_groups(content: str) -> list[list[str]]:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line == SECTION_DELIMITER:
            groups.append(current)
            current = []
        else:
            current.append(line)
    groups.append(current)
    return groups


def _section_body(lines: list[str], heading: str, path: Path | None = None) -> list[str]:
    heading_index = _heading_index(lines)
    if heading_index is None:
        raise PageParseError(_message(path, f"Missing {heading} section"))
    if lines[heading_index] != heading:
        raise PageParseError(_message(path, f"Expected {heading} section"))

    body = lines[heading_index + 1 :]
    return _strip_blank_edges(body)


def _heading_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line == "":
            continue
        return index
    return None


def _strip_blank_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and lines[start] == "":
        start += 1
    while end > start and lines[end - 1] == "":
        end -= 1
    return lines[start:end]


def _parse_sources(lines: list[str]) -> list[str]:
    sources: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            sources.append(stripped[2:])
        else:
            sources.append(stripped)
    return sources


def _message(path: Path | None, detail: str) -> str:
    if path is None:
        return detail
    return f"{detail} in {path}"
