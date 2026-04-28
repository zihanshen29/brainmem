import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.exceptions import PageParseError

ENTRY_PATTERN = re.compile(r"^- (?P<date>\S+) \[event:(?P<event_id>[^\]]+)\]: (?P<description>.+)$")


class TimelineEntry(BaseModel):
    """Structured representation of one timeline bullet."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)

    @field_validator("date", "event_id", "description")
    @classmethod
    def validate_single_line(cls, value: str) -> str:
        """Keep timeline entries renderable as a single markdown line."""
        if "\n" in value or "\r" in value:
            raise ValueError("timeline entry fields must be single-line strings")
        return value


def parse_entry(line: str) -> TimelineEntry:
    """Parse a canonical timeline bullet."""
    match = ENTRY_PATTERN.fullmatch(line)
    if match is None:
        raise PageParseError(f"Invalid timeline entry: {line!r}")
    return TimelineEntry.model_validate(match.groupdict())


def format_entry(entry: TimelineEntry) -> str:
    """Render a timeline entry as a canonical markdown bullet."""
    validated = TimelineEntry.model_validate(entry)
    return f"- {validated.date} [event:{validated.event_id}]: {validated.description}"
