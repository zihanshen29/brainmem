from pathlib import Path

import frontmatter

from brain.exceptions import PageParseError
from brain.models import Page
from brain.pages.parser import (
    COMPILED_TRUTH_HEADING,
    SECTION_DELIMITER,
    SOURCES_HEADING,
    TIMELINE_HEADING,
    _heading_index,
    _split_section_groups,
)
from brain.pages.timeline import TimelineEntry, format_entry


def write_page(path: Path, page: Page) -> None:
    """Render a page to markdown with YAML frontmatter and LF newlines."""
    validated = Page.model_validate(page)
    metadata = validated.frontmatter.model_dump(mode="json", exclude_none=True)
    body = _render_body(validated)
    rendered = frontmatter.dumps(frontmatter.Post(body, **metadata), sort_keys=False)
    _write_lf(path, _ensure_final_lf(rendered))


def append_timeline(path: Path, entry: TimelineEntry) -> None:
    """Append a formatted entry to the Timeline section without rewriting other sections."""
    prefix, body = _split_frontmatter(path)
    groups = _validated_groups(body, path)
    timeline_group = groups[1]
    heading_index = _expect_heading(timeline_group, TIMELINE_HEADING, path)
    insert_at = _content_end(timeline_group, heading_index)
    line = format_entry(entry)

    if insert_at == heading_index + 1:
        timeline_group[insert_at:insert_at] = ["", line]
    else:
        timeline_group.insert(insert_at, line)

    _write_lf(path, _ensure_final_lf(prefix + _join_groups(groups)))


def update_compiled_truth(path: Path, new_text: str) -> None:
    """Replace only the Compiled truth section body."""
    prefix, body = _split_frontmatter(path)
    groups = _validated_groups(body, path)
    compiled_group = groups[0]
    heading_index = _expect_heading(compiled_group, COMPILED_TRUTH_HEADING, path)
    groups[0] = _replace_section_body(compiled_group, heading_index, new_text)
    _write_lf(path, _ensure_final_lf(prefix + _join_groups(groups)))


def update_sources(path: Path, sources: list[str]) -> None:
    """Rewrite or create the Sources section."""
    prefix, body = _split_frontmatter(path)
    groups = _validated_groups(body, path)
    source_lines = _render_sources(sources)

    if len(groups) == 3:
        source_group = groups[2]
        heading_index = _expect_heading(source_group, SOURCES_HEADING, path)
        groups[2] = _replace_section_body(source_group, heading_index, "\n".join(source_lines))
    else:
        groups.append(_new_section(SOURCES_HEADING, source_lines))

    _write_lf(path, _ensure_final_lf(prefix + _join_groups(groups)))


def _render_body(page: Page) -> str:
    return f"\n{SECTION_DELIMITER}\n".join(
        [
            _render_text_section(COMPILED_TRUTH_HEADING, page.compiled_truth),
            _render_list_section(TIMELINE_HEADING, page.timeline, bullet=False),
            _render_list_section(SOURCES_HEADING, page.sources, bullet=True),
        ]
    )


def _render_text_section(heading: str, text: str) -> str:
    stripped = text.strip("\n")
    if not stripped:
        return heading
    return f"{heading}\n\n{stripped}"


def _render_list_section(heading: str, lines: list[str], *, bullet: bool) -> str:
    rendered_lines = _render_sources(lines) if bullet else lines
    if not rendered_lines:
        return heading
    return f"{heading}\n\n" + "\n".join(rendered_lines)


def _render_sources(sources: list[str]) -> list[str]:
    rendered: list[str] = []
    for source in sources:
        source_text = source.strip()
        if not source_text:
            continue
        rendered.append(source_text if source_text.startswith("- ") else f"- {source_text}")
    return rendered


def _split_frontmatter(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0] not in {"---\n", "---"}:
        raise PageParseError(f"Missing YAML frontmatter in {path}")

    for index in range(1, len(lines)):
        if lines[index] in {"---\n", "---"}:
            return "".join(lines[: index + 1]), "".join(lines[index + 1 :])

    raise PageParseError(f"Unterminated YAML frontmatter in {path}")


def _validated_groups(body: str, path: Path) -> list[list[str]]:
    groups = _split_section_groups(body)
    if len(groups) not in {2, 3}:
        raise PageParseError(f"Page body must contain Compiled truth and Timeline sections in {path}")

    _expect_heading(groups[0], COMPILED_TRUTH_HEADING, path)
    _expect_heading(groups[1], TIMELINE_HEADING, path)
    if len(groups) == 3:
        _expect_heading(groups[2], SOURCES_HEADING, path)
    return groups


def _expect_heading(lines: list[str], heading: str, path: Path) -> int:
    heading_index = _heading_index(lines)
    if heading_index is None or lines[heading_index] != heading:
        raise PageParseError(f"Expected {heading} section in {path}")
    return heading_index


def _content_end(lines: list[str], heading_index: int) -> int:
    end = len(lines)
    while end > heading_index + 1 and lines[end - 1] == "":
        end -= 1
    return end


def _replace_section_body(lines: list[str], heading_index: int, text: str) -> list[str]:
    prefix = lines[: heading_index + 1]
    body_lines = text.strip("\n").split("\n") if text.strip("\n") else []
    if not body_lines:
        return prefix
    return [*prefix, "", *body_lines]


def _new_section(heading: str, body_lines: list[str]) -> list[str]:
    if not body_lines:
        return [heading]
    return [heading, "", *body_lines]


def _join_groups(groups: list[list[str]]) -> str:
    return f"\n{SECTION_DELIMITER}\n".join("\n".join(group) for group in groups)


def _ensure_final_lf(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if normalized.endswith("\n") else f"{normalized}\n"


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
