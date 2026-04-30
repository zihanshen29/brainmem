import sqlite3


def get_stat(conn: sqlite3.Connection, key: str) -> str | None:
    """Return a stat value by key, or None when missing."""
    row = conn.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return row["value"]


def set_stat(conn: sqlite3.Connection, key: str, value: str | int | float) -> None:
    """Set a stat value, creating the key when needed."""
    conn.execute(
        """
        INSERT INTO stats (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, str(value)),
    )


def increment_stat(conn: sqlite3.Connection, key: str, amount: int | float = 1) -> str:
    """Increment a numeric stat and return the new value as stored."""
    current = get_stat(conn, key)
    current_number = float(current) if current is not None else 0.0
    next_number = current_number + amount
    next_value = str(int(next_number)) if next_number.is_integer() else str(next_number)
    set_stat(conn, key, next_value)
    return next_value
