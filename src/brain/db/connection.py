import importlib
import sqlite3
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlencode

from brain.exceptions import DBError


def prepare_sqlite_extension() -> None:
    """Initialize optional native modules before a Windows stdin reader starts.

    sqlite_vec imports NumPy when installed. Its Windows native initialization
    can wait on a CRT lock held by a blocking stdin reader, so MCP must warm it
    before opening the transport. Missing optional support still falls back.
    """
    with suppress(ImportError):
        importlib.import_module("sqlite_vec")


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with brain defaults.

    Args:
        path: Path to the SQLite database file.
        read_only: Refuse writes and missing databases without changing journal mode.

    Returns:
        Configured SQLite connection.

    Raises:
        DBError: If SQLite cannot open or configure the connection.
    """
    conn = None
    try:
        conn = (
            sqlite3.connect(sqlite_uri(path, mode="ro"), uri=True)
            if read_only else sqlite3.connect(Path(path))
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.enable_load_extension(True)
        try:
            import sqlite_vec

            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except (ImportError, sqlite3.Error) as exc:
        if conn is not None:
            conn.close()
        raise DBError(f"Could not connect to database: {path}") from exc
    return conn


def sqlite_uri(path: Path, **params: str | int) -> str:
    """Build a SQLite file URI that is valid for absolute Windows paths."""
    uri = Path(path).expanduser().resolve().as_uri()
    if not params:
        return uri
    return f"{uri}?{urlencode(params)}"
