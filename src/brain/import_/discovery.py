from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel

from brain.models.import_job import ImportFileKind

KIND_BY_SUFFIX: dict[str, ImportFileKind] = {
    ".md": "md",
    ".txt": "txt",
    ".pdf": "pdf",
    ".jsonl": "jsonl",
}


class DiscoveredFile(BaseModel):
    """A supported source file found during import discovery."""

    path: Path
    relative_path: str
    kind: ImportFileKind
    file_hash: str


def discover_files(path: Path, kinds: set[ImportFileKind] | None = None) -> list[DiscoveredFile]:
    """Recursively discover supported files under path."""
    root = Path(path).expanduser()
    requested = kinds or {"md", "txt", "pdf", "jsonl"}
    files = [_discover_one(root, root, requested)] if root.is_file() else _discover_many(root, requested)
    return sorted((file for file in files if file is not None), key=lambda file: file.relative_path)


def _discover_many(root: Path, kinds: set[ImportFileKind]) -> list[DiscoveredFile | None]:
    return [_discover_one(candidate, root, kinds) for candidate in root.rglob("*") if candidate.is_file()]


def _discover_one(path: Path, root: Path, kinds: set[ImportFileKind]) -> DiscoveredFile | None:
    kind = KIND_BY_SUFFIX.get(path.suffix.lower())
    if kind is None or kind not in kinds:
        return None
    return DiscoveredFile(
        path=path,
        relative_path=path.relative_to(root).as_posix() if path != root else path.name,
        kind=kind,
        file_hash=_sha256_file(path),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
