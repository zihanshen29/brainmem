from pathlib import Path

import pytest

from brain.db.connection import connect
from brain.db.migrations import init_db
from brain.db.stats import get_stat, increment_stat, set_stat
from brain.exceptions import DBError

pytest.importorskip("sqlite_vec")


def test_migration_upgrades_baseline_to_phase2(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.db"

    init_db(db_path)

    with connect(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
        index_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()

    tables = {row["name"] for row in table_rows}
    indexes = {row["name"] for row in index_rows}

    assert version == 2
    assert {
        "entities",
        "entity_aliases",
        "facts",
        "backlinks",
        "tier_proposals",
        "ingest_cursor",
        "lint_results",
        "embeddings",
        "embedding_index",
        "import_jobs",
        "import_files",
        "stats",
    }.issubset(tables)
    assert {"idx_embedding_index_page", "idx_embedding_index_hash"}.issubset(indexes)


def test_stats_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.db"
    init_db(db_path)

    with connect(db_path) as conn:
        assert get_stat(conn, "total_embedding_tokens") == "0"

        set_stat(conn, "total_embedding_tokens", 10)
        assert get_stat(conn, "total_embedding_tokens") == "10"

        assert increment_stat(conn, "total_embedding_tokens", 5) == "15"
        assert increment_stat(conn, "total_cost_usd", 0.25) == "0.25"


def test_failed_migration_rolls_back_user_version(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "brain.db"
    init_db(db_path)

    with connect(db_path) as conn:
        conn.executescript(
            """
            BEGIN;
            DROP TABLE stats;
            PRAGMA user_version = 1;
            COMMIT;
            """
        )

    bad_sql = "CREATE TABLE stats (key TEXT PRIMARY KEY); SELECT * FROM missing_table;"

    def fake_read_text(self, encoding=None):
        if self.name == "0002_phase2.sql":
            return bad_sql
        return Path.read_text(self, encoding=encoding)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    with pytest.raises(DBError):
        init_db(db_path)

    with connect(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        stats_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stats'"
        ).fetchone()

    assert version == 1
    assert stats_exists is None
