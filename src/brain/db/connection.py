import sqlite3
from pathlib import Path

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
    try:
        conn = sqlite3.connect(Path(path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error as exc:
        raise DBError(f"Could not connect to database: {path}") from exc
    return conn
