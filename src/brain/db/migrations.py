import sqlite3
from pathlib import Path

from brain.db.connection import connect
from brain.exceptions import DBError

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def _migration_version(path: Path) -> int:
    """Extract numeric version from a migration filename."""
    prefix = path.stem.split("_", maxsplit=1)[0]
    return int(prefix)


def init_db(path: Path) -> None:
    """Initialize a brain SQLite database and run pending migrations.

    Args:
        path: Path to the SQLite database file.

    Raises:
        DBError: If migrations cannot be read or applied.
    """
    conn = None
    try:
        migrations = sorted(MIGRATIONS_DIR.glob("*.sql"), key=_migration_version)
        conn = connect(path)
        with conn:
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            for migration in migrations:
                version = _migration_version(migration)
                if version <= current_version:
                    continue
                sql = migration.read_text(encoding="utf-8")
                conn.executescript(sql)
                conn.execute(f"PRAGMA user_version = {version}")
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise DBError(f"Could not initialize database: {path}") from exc
    finally:
        if conn is not None:
            conn.close()
