import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from brain.exceptions import DBError


def connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with brain defaults.

    Args:
        path: Path to the SQLite database file.

    Returns:
        Configured SQLite connection.

    Raises:
        DBError: If SQLite cannot open or configure the connection.
    """
    conn = None
    try:
        conn = sqlite3.connect(Path(path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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
