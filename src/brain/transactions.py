"""Recoverable local file/SQLite units under the root write lock.

Files are snapshotted before mutation. The SQLite commit marker decides recovery:
an uncommitted unit restores its files, a committed unit keeps them. Readers refuse
an interrupted unit until a writer recovers it. Only cooperating processes are covered.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from brain.exceptions import BrainError

_active: ContextVar[tuple[Path, Path, dict] | None] = ContextVar("brain_file_unit", default=None)


def _hash(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def protect_path(path: Path, after: bytes | None) -> None:
    active = _active.get()
    if active is None:
        return
    root, directory, manifest = active
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    if relative not in manifest["files"]:
        before = path.read_bytes() if path.is_file() else None
        if before is not None:
            saved = directory / "before" / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            with saved.open("wb") as stream:
                stream.write(before)
                stream.flush()
                os.fsync(stream.fileno())
        manifest["files"][relative] = {"before": _hash(before), "states": []}
    manifest["files"][relative]["after"] = _hash(after)
    manifest["files"][relative]["states"].append(_hash(after))
    _atomic_text(directory / "manifest.json", json.dumps(manifest))


def atomic_text(path: Path, text: str) -> None:
    protect_path(path, text.encode("utf-8"))
    _atomic_text(path, text)


def _atomic_text(path: Path, text: str) -> None:
    _atomic_bytes(path, text.encode("utf-8"))


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_operation_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS operation_commits (id TEXT PRIMARY KEY)")
    conn.commit()


def completed(conn: sqlite3.Connection, key: str) -> bool:
    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'operation_commits'").fetchone()
        is None
    ):
        return False
    return (
        conn.execute("SELECT 1 FROM operation_commits WHERE id = ?", (key,)).fetchone() is not None
    )


def _restore(root: Path, directory: Path, entries: dict) -> None:
    # Check all files before restoring any. Never overwrite a later manual edit.
    for name, state in entries.items():
        target = root / name
        if not target.resolve().is_relative_to(root.resolve()):
            raise BrainError("Invalid recovery path")
        current = _hash(target.read_bytes() if target.is_file() else None)
        if current not in {state["before"], *state["states"]}:
            raise BrainError(f"Recovery stopped: {name} changed outside the interrupted operation")
        if (
            state["before"] is not None
            and _hash((directory / "before" / name).read_bytes()) != state["before"]
        ):
            raise BrainError(f"Recovery snapshot checksum mismatch: {name}")
    for name, state in entries.items():
        target = root / name
        if state["before"] is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_bytes(target, (directory / "before" / name).read_bytes())


def recover_pending(root: Path, *, write: bool) -> None:
    parent = root / ".brainmem" / "transactions"
    if not parent.is_dir():
        return
    for directory in sorted(parent.iterdir()):
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue  # A snapshot interrupted before publication changed no user files.
        if not write:
            raise BrainError(
                "Interrupted write needs recovery; run mem recover --brain-root <root>"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        from brain.db.connection import connect

        conn = connect(root / "brain.db")
        try:
            if not completed(conn, manifest["id"]):
                _restore(root, directory, manifest["files"])
        finally:
            conn.close()
        shutil.rmtree(directory)


@contextmanager
def durable_unit(root: Path, conn: sqlite3.Connection, key: str):
    """Callers must not commit the connection or touch unrelated files inside this unit."""
    if _active.get() is not None:
        raise BrainError("Nested file/SQLite transactions are not supported")
    ensure_operation_table(conn)
    directory = root / ".brainmem" / "transactions" / uuid.uuid4().hex
    manifest: dict = {"id": key, "files": {}}
    _atomic_text(directory / "manifest.json", json.dumps(manifest))
    token = _active.set((root, directory, manifest))
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.execute("INSERT INTO operation_commits (id) VALUES (?)", (key,))
        conn.commit()
    except BaseException:
        conn.rollback()
        _restore(root, directory, manifest["files"])
        shutil.rmtree(directory)
        raise
    else:
        shutil.rmtree(directory)
    finally:
        _active.reset(token)
