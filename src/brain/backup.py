"""Complete local snapshots and non-destructive restore verification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from brain.concurrency import root_lock
from brain.db.connection import connect
from brain.exceptions import BrainError

MANIFEST = "brainmem-backup-manifest.json"


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_manifest(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise BrainError(f"Snapshot refuses filesystem links: {path.relative_to(root)}")
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        if name.endswith(("brain.db-shm", ".lock")) or name.startswith(".brainmem/transactions/"):
            continue
        result[name] = file_hash(path)
    return result


def create_backup(root: Path, destination: Path) -> dict:
    root, destination = root.resolve(), destination.resolve()
    if destination.is_relative_to(root) or destination.exists():
        raise BrainError("Backup destination must be a new file outside the brain root")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        root_lock(root),
        tempfile.TemporaryDirectory(prefix="brainmem-backup-", dir=destination.parent) as temporary,
    ):
        source = source_manifest(root)
        snapshot_db = Path(temporary) / "brain.db"
        with (
            closing(connect(root / "brain.db", read_only=True)) as conn,
            closing(sqlite3.connect(snapshot_db)) as snapshot,
        ):
            conn.backup(snapshot)
        files = {}
        staged = Path(temporary) / "snapshot.zip"
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in source:
                if name in {"brain.db-wal", "brain.db-shm"}:
                    continue
                path = snapshot_db if name == "brain.db" else root / name
                archive.write(path, name)
                files[name] = file_hash(path)
            manifest = {
                "format": 1,
                "created": datetime.now(UTC).isoformat(),
                "files": files,
                "source_files": source,
                "mtime_ns": {name: (root / name).stat().st_mtime_ns for name in files},
            }
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        if source_manifest(root) != source:
            raise BrainError(
                "Files changed outside BrainMem during backup; snapshot was not published"
            )
        verify_backup(staged)
        os.replace(staged, destination)
    return {
        "path": str(destination),
        "files": len(files),
        "verified": True,
        "created": manifest["created"],
        "sha256": file_hash(destination),
    }


def _extract_verified(archive_path: Path, target: Path) -> dict:
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len({name.casefold() for name in names}):
            raise BrainError("Backup has duplicate entries")
        if MANIFEST not in names:
            raise BrainError("Not a BrainMem backup")
        manifest = json.loads(archive.read(MANIFEST))
        if manifest.get("format") != 1:
            raise BrainError("Unsupported backup format")
        files = manifest["files"]
        if set(names) != set(files) | {MANIFEST}:
            raise BrainError("Backup manifest does not match archive entries")
        for name, digest in files.items():
            safe = PurePosixPath(name)
            if (
                safe.is_absolute()
                or ".." in safe.parts
                or "\\" in name
                or ":" in name
                or any(part.endswith((".", " ")) for part in safe.parts)
            ):
                raise BrainError("Unsafe backup path")
            destination = target / name
            if not destination.resolve().is_relative_to(target.resolve()):
                raise BrainError("Unsafe restore destination")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            if file_hash(destination) != digest:
                raise BrainError(f"Backup checksum mismatch: {name}")
            modified = manifest.get("mtime_ns", {}).get(name)
            if isinstance(modified, int) and modified >= 0:
                os.utime(destination, ns=(modified, modified))
            else:
                # Older archives still carry ZIP timestamps at two-second precision.
                timestamp = datetime(*archive.getinfo(name).date_time).timestamp()
                os.utime(destination, (timestamp, timestamp))
    with closing(connect(target / "brain.db", read_only=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BrainError("Backup database failed integrity check")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BrainError("Backup database failed foreign key check")
    from brain.ledger import read_all

    list(read_all(target / "events.jsonl"))
    return manifest


def verify_backup(archive_path: Path) -> dict:
    with tempfile.TemporaryDirectory(
        prefix="brainmem-verify-", dir=archive_path.parent
    ) as temporary:
        manifest = _extract_verified(archive_path, Path(temporary))
    return {"verified": True, "files": len(manifest["files"]), "created": manifest["created"]}


def restore_backup(archive_path: Path, destination: Path) -> dict:
    destination = destination.resolve()
    if destination.exists():
        raise BrainError("Restore requires a new destination; existing data is never overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="brainmem-restore-", dir=destination.parent
    ) as temporary:
        staged = Path(temporary) / "restored"
        staged.mkdir()
        manifest = _extract_verified(archive_path, staged)
        os.replace(staged, destination)
    return {"restored": str(destination), "files": len(manifest["files"]), "verified": True}


def require_current_backup(root: Path, archive_path: Path) -> None:
    verify_backup(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read(MANIFEST))
    if source_manifest(root) != manifest["source_files"]:
        raise BrainError(
            "Backup is stale or belongs to another root; create a fresh backup before applying"
        )
