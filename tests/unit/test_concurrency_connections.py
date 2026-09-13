import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from brain.config_context import configured_path, get_config_path
from brain.db.connection import connect
from brain.llm import client
from brain.pipeline.ingest import _configured_llm_path


def test_reader_rejects_writes_and_sees_committed_wal(tmp_path: Path) -> None:
    path = tmp_path / "readers.db"
    writer = connect(path)
    try:
        writer.execute("CREATE TABLE sample (value TEXT)")
        writer.execute("INSERT INTO sample VALUES ('committed')")
        writer.commit()
        reader = connect(path, read_only=True)
        try:
            assert reader.execute("SELECT value FROM sample").fetchone()[0] == "committed"
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("INSERT INTO sample VALUES ('forbidden')")
            assert reader.execute("SELECT vec_version()").fetchone()[0]
        finally:
            reader.close()
    finally:
        writer.close()


def test_reader_does_not_create_database_or_change_journal(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    from brain.exceptions import DBError

    with pytest.raises(DBError):
        connect(missing, read_only=True)
    assert not missing.exists()
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    reader = connect(path, read_only=True)
    try:
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        reader.close()


def test_provider_config_is_thread_local_and_nesting_restores(monkeypatch, tmp_path: Path) -> None:
    fallback = str(tmp_path / "fallback.toml")
    monkeypatch.setenv("BRAIN_CONFIG", fallback)
    monkeypatch.setattr(client, "_settings_from_config", lambda path, **kwargs: path)
    barrier = threading.Barrier(2)

    def resolve(name: str) -> Path:
        path = tmp_path / name / "config.toml"
        with _configured_llm_path(path):
            barrier.wait(timeout=5)
            assert get_config_path() == str(path)
            assert os.environ["BRAIN_CONFIG"] == fallback
            with configured_path(tmp_path / "nested.toml"):
                assert client._resolve_llm_settings() == tmp_path / "nested.toml"
            assert client._resolve_llm_settings() == path
            barrier.wait(timeout=5)
        assert get_config_path() == fallback
        return path

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resolve, ["one", "two"]))
    assert results == [tmp_path / name / "config.toml" for name in ("one", "two")]
    assert get_config_path() == fallback
