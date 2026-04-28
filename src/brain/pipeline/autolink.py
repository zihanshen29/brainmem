import re
from datetime import UTC, datetime

from brain.models import Backlink, EntityType, PageType

_WIKILINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _coerce_page_type(value: PageType | None) -> PageType | None:
    if value is None or isinstance(value, PageType):
        return value
    try:
        return PageType(value)
    except ValueError:
        return None


def _coerce_entity_type(value: EntityType | None) -> EntityType | None:
    if value is None or isinstance(value, EntityType):
        return value
    try:
        return EntityType(value)
    except ValueError:
        return None


def _alias_relation(
    from_page_type: PageType | None,
    entity_type: EntityType | None,
) -> str:
    if from_page_type is PageType.PROJECT and entity_type is EntityType.PERSON:
        return "works_with"
    if from_page_type is PageType.PROJECT and entity_type is EntityType.CONCEPT:
        return "involves"
    if from_page_type is PageType.EVENT and entity_type is EntityType.PERSON:
        return "attended_by"
    if from_page_type is PageType.CONVERSATION and entity_type is EntityType.PERSON:
        return "participant"
    return "mentions"


def _inside_span(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in spans)


def _alias_occurrences(
    line: str,
    alias_map: dict[str, str],
    wikilink_spans: list[tuple[int, int]],
) -> list[tuple[int, str]]:
    occurrences: list[tuple[int, str]] = []
    for alias, entity_id in alias_map.items():
        if not alias:
            continue

        start = 0
        while True:
            index = line.find(alias, start)
            if index == -1:
                break
            if not _inside_span(index, wikilink_spans):
                occurrences.append((index, entity_id))
            start = index + len(alias)
    return occurrences


def extract_backlinks(
    content: str,
    alias_map: dict[str, str],
    from_page: str = "unknown",
    from_page_type: PageType | None = None,
    entity_types: dict[str, EntityType] | None = None,
) -> list[Backlink]:
    """Extract deterministic alias and wikilink backlinks from markdown content."""
    extracted_at = _now_utc()
    source_type = _coerce_page_type(from_page_type)
    target_types = entity_types or {}
    links: dict[tuple[str, str, str], Backlink] = {}

    for line_number, line in enumerate(content.splitlines(), start=1):
        wikilink_matches = list(_WIKILINK_PATTERN.finditer(line))
        wikilink_spans = [match.span() for match in wikilink_matches]
        occurrences: list[tuple[int, str, str]] = []

        for index, entity_id in _alias_occurrences(line, alias_map, wikilink_spans):
            entity_type = _coerce_entity_type(target_types.get(entity_id))
            occurrences.append((index, entity_id, _alias_relation(source_type, entity_type)))

        for match in wikilink_matches:
            entity_id = match.group(1).strip()
            if entity_id:
                occurrences.append((match.start(), entity_id, "mentions"))

        for _, to_entity, relation in sorted(occurrences, key=lambda item: item[0]):
            key = (from_page, to_entity, relation)
            if key in links:
                continue
            links[key] = Backlink(
                from_page=from_page,
                to_entity=to_entity,
                relation=relation,
                line_number=line_number,
                extracted_at=extracted_at,
            )

    return list(links.values())
