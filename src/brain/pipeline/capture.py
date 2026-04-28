from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
from pydantic import BaseModel, ConfigDict

from brain.config import load_config
from brain.exceptions import BrainError
from brain.paths import BrainPaths

VALID_KINDS = {"note", "chat", "idea", "meeting"}
VALID_SOURCES = {"stdin", "file", "editor"}


class CaptureReport(BaseModel):
    """Summary of one captured laundry item."""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str
    committed: bool = False


def capture(
    brain_root: Path,
    text: str,
    kind: str = "note",
    source: str = "stdin",
    source_ref: str | None = None,
    auto_commit: bool | None = None,
) -> CaptureReport:
    """Capture raw text into laundry without ingesting or appending events."""
    if kind not in VALID_KINDS:
        raise BrainError(f"Unsupported capture kind: {kind}")
    if source not in VALID_SOURCES:
        raise BrainError(f"Unsupported capture source: {source}")
    if not text.strip():
        raise BrainError("Capture content is empty")

    paths = BrainPaths(Path(brain_root))
    config = load_config(paths.config_path)
    captured = _now_utc()
    path = _capture_path(paths.laundry_dir, captured, text)
    relative_path = path.relative_to(paths.root).as_posix()

    metadata = {
        "captured": captured.isoformat(),
        "kind": kind,
        "source": source,
    }
    if source_ref is not None:
        metadata["source_ref"] = source_ref

    _write_lf(path, _render_capture(metadata, text))

    committed = False
    should_commit = config.git.auto_commit if auto_commit is None else auto_commit
    if should_commit:
        from brain import git_ops

        committed = (
            git_ops.commit(
                paths.root,
                f"capture: {kind} {path.name}",
                paths=[path],
            )
            is not None
        )

    return CaptureReport(path=relative_path, kind=kind, committed=committed)


def _capture_path(laundry_dir: Path, captured: datetime, text: str) -> Path:
    stem = f"{captured.strftime('%Y-%m-%d-%H%M%S')}_{_slug_from_text(text)}"
    candidate = laundry_dir / f"{stem}.md"
    if not candidate.exists():
        return candidate

    for index in range(2, 10_000):
        candidate = laundry_dir / f"{stem}_{index}.md"
        if not candidate.exists():
            return candidate
    raise BrainError(f"Could not find capture filename in {laundry_dir}")


def _slug_from_text(text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    words = re.findall(r"[A-Za-z0-9]+", first_line.lower())
    if not words:
        return "capture"
    return "-".join(words[:8])


def _render_capture(metadata: dict[str, str], text: str) -> str:
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    return frontmatter.dumps(frontmatter.Post(body, **metadata), sort_keys=False)


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    path.write_text(normalized, encoding="utf-8", newline="\n")


def _now_utc() -> datetime:
    return datetime.now(UTC)
