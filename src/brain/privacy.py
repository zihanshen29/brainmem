"""Content-level provider boundaries. Local recall always remains available."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

import frontmatter
import yaml  # type: ignore[import-untyped]

from brain.config import load_config
from brain.exceptions import BrainError

_HEADER = re.compile(r"^(source_agent|source_context|source_channel|source_ref|privacy):\s*(.*)$")


def split_provenance(text: str) -> tuple[str, dict[str, Any]]:
    """Separate collection metadata from prose, without rewriting the source."""
    metadata: dict[str, Any] = {}
    body = text.lstrip("\ufeff").strip()

    def merge(labels: dict[str, Any]) -> None:
        for key, value in labels.items():
            previous = metadata.get(key)
            if key != "privacy" or previous is None or previous == "provider-allowed":
                metadata[key] = value

    # Capture and MCP may wrap an already labelled note in their own headers.
    for _ in range(32):
        previous_body = body
        if body.startswith(("---\n", "---\r\n")):
            try:
                post = frontmatter.loads(body)
            except yaml.YAMLError as exc:
                raise BrainError("Invalid source frontmatter") from exc
            merge(post.metadata)
            body = post.content.strip()
        lines = body.splitlines()
        index = 0
        while index < len(lines):
            match = _HEADER.fullmatch(lines[index].strip())
            if match is None:
                break
            merge({match[1]: match[2]})
            index += 1
        body = "\n".join(lines[index:]).strip()
        if body == previous_body:
            return body, metadata
    raise BrainError("Source has too many nested metadata wrappers")


def contains_secret(text: str) -> bool:
    from brain.integrations.codex.hook import contains_secret as detect

    return detect(text)


def external_allowed(
    root: Path,
    *,
    path: Path | str | None = None,
    text: str = "",
    metadata: dict[str, Any] | None = None,
    _seen: set[Path] | None = None,
) -> bool:
    config = load_config(root / "config.toml")
    if config.privacy.default == "local-only" or contains_secret(text):
        return False
    _, header = split_provenance(text)
    for labels in (header, metadata or {}):
        if labels.get("privacy") is not None and labels.get("privacy") != "provider-allowed":
            return False
    if path is None:
        return True
    if str(path).startswith(("event:", "events.jsonl:")):
        from brain.ledger import read_all

        event_id = str(path).split(":", 1)[1]
        seen = _seen if _seen is not None else set()
        marker = root.resolve() / ".event-policy" / event_id
        if marker in seen:
            return True
        seen.add(marker)
        for event in read_all(root / "events.jsonl"):
            if event.id == event_id:
                return external_allowed(
                    root,
                    path=event.raw_payload_path,
                    text=event.raw_payload or "",
                    metadata=event.metadata,
                    _seen=seen,
                )
        return False
    candidate = Path(path)
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        relative = candidate.relative_to(root.resolve()).as_posix()
    except ValueError:
        return False
    if any(
        fnmatch.fnmatchcase(relative.casefold(), pattern.casefold())
        for pattern in config.privacy.local_only_paths
    ):
        return False
    seen = _seen if _seen is not None else set()
    if candidate in seen or not candidate.is_file():
        return True
    seen.add(candidate)
    try:
        raw = candidate.read_text(encoding="utf-8")
        _, labels = split_provenance(raw)
        if (
            labels.get("privacy") is not None and labels.get("privacy") != "provider-allowed"
        ) or contains_secret(raw):
            return False
        if relative.startswith("pages/") and candidate.suffix == ".md":
            from brain.pages import parse_page

            for source in parse_page(candidate).sources:
                if not source.startswith(("https://", "http://")) and not external_allowed(
                    root, path=source, _seen=seen
                ):
                    return False
    except (OSError, UnicodeError, BrainError, ValueError):
        return False
    return True


def require_external(root: Path, **kwargs: Any) -> None:
    if not external_allowed(root, **kwargs):
        raise BrainError(
            "Content is local-only or contains a possible secret; provider access refused"
        )
