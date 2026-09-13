from __future__ import annotations

import multiprocessing
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from brain.db.connection import connect, sqlite_uri
from brain.ledger import read_all
from brain.mcp import tools
from brain.pages import parse_page
from brain.pipeline.procedure import create_procedure


def _record_runs(root: str, channel: str, ready: Any, start: Any, results: Any) -> None:
    os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
    ready.put(channel)
    if not start.wait(15):
        results.put("start timed out")
        return
    try:
        for index in range(3):
            if channel == "mcp":
                tools.brain_procedure_run(
                    "concurrent", "success", f"mcp run {index}", brain_root=root
                )
            else:
                from typer.testing import CliRunner

                from brain.cli.main import app

                result = CliRunner().invoke(
                    app,
                    [
                        "procedure", "run", "concurrent", "--brain-root", root,
                        "--result", "success", "--note", f"cli run {index}",
                    ],
                )
                if result.exit_code:
                    raise AssertionError(result.output)
        results.put("ok")
    except Exception as exc:
        results.put(f"{type(exc).__name__}: {exc}")


def test_cli_and_mcp_processes_preserve_procedure_counts_and_events(brain_root: Path) -> None:
    create_procedure(brain_root, "concurrent", title="Concurrent runs", auto_commit=False)
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    start = context.Event()
    processes = [
        context.Process(target=_record_runs, args=(str(brain_root), channel, ready, start, results))
        for channel in ["cli", "mcp", "cli", "mcp"]
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            ready.get(timeout=20)
        start.set()
        outcomes = [results.get(timeout=40) for _ in processes]
        assert outcomes == ["ok"] * len(processes)
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        ready.close()
        results.close()

    page = parse_page(brain_root / "pages" / "procedures" / "concurrent.md")
    events = list(read_all(brain_root / "events.jsonl"))
    assert page.frontmatter.success_count == 12
    assert len(page.timeline) == len(events) == 12
    assert len({event.id for event in events}) == 12
    assert all(event.id in "\n".join(page.timeline) for event in events)


@pytest.mark.parametrize(
    "finalizer",
    [
        "brain.pipeline.ingest._close_ingest_connection",
        "brain.pipeline.rebuild._finalize_db",
        "brain.pipeline.entity_merge._finalize_db",
        "brain.import_.importer._close_connection",
    ],
)
def test_finalizers_preserve_live_wal_snapshot(
    tmp_path: Path, finalizer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external SQLite reader can outlive a cooperating writer operation."""
    from importlib import import_module

    original_unlink = Path.unlink

    def protected_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        assert not path.name.endswith(("-wal", "-shm")), "SQLite owns live sidecar files"
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", protected_unlink)
    path = tmp_path / "brain.db"
    with closing(connect(path)) as writer:
        writer.execute("PRAGMA busy_timeout = 1")
        writer.execute("CREATE TABLE sample (value TEXT)")
        writer.execute("INSERT INTO sample VALUES ('old')")
        writer.commit()
        with closing(sqlite3.connect(sqlite_uri(path, mode="ro"), uri=True)) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT value FROM sample").fetchone() == ("old",)
            writer.execute("UPDATE sample SET value = 'new'")
            writer.commit()
            module, name = finalizer.rsplit(".", 1)
            getattr(import_module(module), name)(writer, path)
            assert path.with_name("brain.db-wal").exists()
            assert reader.execute("SELECT value FROM sample").fetchone() == ("old",)
            with closing(sqlite3.connect(sqlite_uri(path, mode="ro"), uri=True)) as latest:
                assert latest.execute("SELECT value FROM sample").fetchone() == ("new",)


def test_read_tools_leave_canonical_brain_files_unchanged(brain_root: Path) -> None:
    def snapshot() -> dict[str, bytes]:
        return {
            path.relative_to(brain_root).as_posix(): path.read_bytes()
            for path in brain_root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(brain_root).parts
            # SQLite mode=ro may manage WAL/SHM bookkeeping itself. Canonical
            # DB/Markdown/ledger bytes and the rest of the file set stay fixed.
            and path.name not in {"brain.db-wal", "brain.db-shm"}
        }

    before = snapshot()
    tools.brain_status(brain_root)
    tools.brain_ask("local query", brain_root=brain_root)
    tools.brain_inject("local query", brain_root=brain_root)
    tools.brain_procedure_list(brain_root)
    tools.brain_recent_events(brain_root)
    assert snapshot() == before


def test_init_git_environment_is_request_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from brain.cli import init

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "outer-git-config")
    observed: list[str] = []

    def fake_commit(root: Path, message: str) -> None:
        observed.append(os.environ["GIT_CONFIG_GLOBAL"])
        assert init._git_env()["GIT_CONFIG_GLOBAL"] == os.devnull

    monkeypatch.setattr(init, "_fallback_commit", fake_commit)
    init._commit_initial_repository(tmp_path)
    assert observed == ["outer-git-config"]


def test_cli_import_status_reads_committed_wal(brain_root: Path) -> None:
    from brain.cli.import_ import _readonly_jobs_connection

    with closing(connect(brain_root / "brain.db")) as writer:
        writer.execute("CREATE TABLE concurrency_marker(value TEXT)")
        writer.execute("INSERT INTO concurrency_marker VALUES ('visible')")
        writer.commit()
        with closing(_readonly_jobs_connection(brain_root)) as reader:
            assert reader.execute("SELECT value FROM concurrency_marker").fetchone()[0] == "visible"


def test_mcp_reader_waits_for_complete_ledger_and_page_update(
    brain_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brain import concurrency
    from brain.pipeline import procedure

    create_procedure(brain_root, "concurrent", title="Concurrent run", auto_commit=False)
    staged, release, read_attempted = threading.Event(), threading.Event(), threading.Event()
    original_write = procedure.write_page
    original_lock = concurrency._file_lock

    def paused_write(path: Path, page: Any) -> None:
        # run_procedure has appended the ledger event, but not its page yet.
        staged.set()
        assert release.wait(10)
        original_write(path, page)

    def observed_lock(path: Path, *, shared: bool, deadline: float) -> Any:
        if shared:
            read_attempted.set()
        return original_lock(path, shared=shared, deadline=deadline)

    monkeypatch.setattr(procedure, "write_page", paused_write)
    monkeypatch.setattr(concurrency, "_file_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            tools.brain_procedure_run,
            "concurrent", "success", "one complete update", brain_root=brain_root,
        )
        try:
            assert staged.wait(5)
            reader = executor.submit(tools.brain_procedure_list, brain_root)
            assert read_attempted.wait(5)
            assert not reader.done()
        finally:
            release.set()
        assert writer.result(timeout=5)["success_count"] == 1
        assert reader.result(timeout=5)["procedures"][0]["success_count"] == 1
