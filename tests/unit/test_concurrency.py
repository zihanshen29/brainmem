from __future__ import annotations

import inspect
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from brain.concurrency import RootBusyError, coordinated, root_lock
from brain.config_context import get_config_path
from brain.exceptions import BrainError


def _hold_lock(
    root: str,
    write: bool,
    entered: Any,
    release: Any,
    results: Any,
    data_waiting: Any = None,
) -> None:
    if data_waiting is not None:
        # Signal after the writer owns the turnstile, before it waits for readers.
        # This observes a real acquisition phase instead of guessing with a sleep.
        import brain.concurrency as concurrency

        original_file_lock = concurrency._file_lock

        def observed_file_lock(path: Path, *, shared: bool, deadline: float) -> Any:
            if path.suffix == ".lock" and not shared:
                data_waiting.set()
            return original_file_lock(path, shared=shared, deadline=deadline)

        concurrency._file_lock = observed_file_lock

    try:
        with root_lock(root, write=write, timeout=5.0):
            entered.set()
            if not release.wait(10.0):
                raise TimeoutError("test did not release lock holder")
        results.put("released")
    except Exception as exc:
        results.put(f"error: {type(exc).__name__}: {exc}")


def _attempt_lock(root: str, write: bool, timeout: float, results: Any) -> None:
    try:
        with root_lock(root, write=write, timeout=timeout):
            results.put("acquired")
    except RootBusyError:
        results.put("busy")
    except Exception as exc:
        results.put(f"error: {type(exc).__name__}: {exc}")


@pytest.fixture()
def lock_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BRAINMEM_LOCK_DIR", str(tmp_path / "operation-locks"))
    root = tmp_path / "brain"
    root.mkdir()
    return root


@pytest.fixture()
def spawned() -> Any:
    context = multiprocessing.get_context("spawn")
    processes: list[Any] = []
    queues: list[Any] = []

    def start(target: Any, *args: Any) -> tuple[Any, Any]:
        results = context.Queue()
        process = context.Process(target=target, args=(*args, results))
        processes.append(process)
        queues.append(results)
        process.start()
        return process, results

    yield context, start, processes, queues

    for process in processes:
        process.join(timeout=0.2)
        if process.is_alive():
            process.terminate()
        process.join(timeout=5.0)
        assert not process.is_alive(), "test child did not exit"
    for results in queues:
        results.close()
        results.join_thread()


def test_process_readers_can_hold_the_same_root_together(lock_root: Path, spawned: Any) -> None:
    context, start, _, _ = spawned
    first_entered, second_entered, release = context.Event(), context.Event(), context.Event()
    _, first_results = start(_hold_lock, str(lock_root), False, first_entered, release)
    try:
        assert first_entered.wait(5.0)
        _, second_results = start(_hold_lock, str(lock_root), False, second_entered, release)
        assert second_entered.wait(5.0), "second reader could not overlap the first reader"
    finally:
        release.set()
    assert first_results.get(timeout=5.0) == "released"
    assert second_results.get(timeout=5.0) == "released"


@pytest.mark.parametrize("holder_write,waiter_write", [(False, True), (True, False), (True, True)])
def test_conflicting_processes_time_out_then_acquire_after_release(
    lock_root: Path, spawned: Any, holder_write: bool, waiter_write: bool
) -> None:
    _, start, _, _ = spawned
    with root_lock(lock_root, write=holder_write):
        _, blocked = start(_attempt_lock, str(lock_root), waiter_write, 0.15)
        assert blocked.get(timeout=5.0) == "busy"
    _, available = start(_attempt_lock, str(lock_root), waiter_write, 1.0)
    assert available.get(timeout=5.0) == "acquired"


def test_exception_releases_lock_for_other_processes(lock_root: Path, spawned: Any) -> None:
    _, start, _, _ = spawned
    with pytest.raises(RuntimeError, match="operation failed"), root_lock(lock_root, write=True):
        raise RuntimeError("operation failed")
    _, results = start(_attempt_lock, str(lock_root), True, 1.0)
    assert results.get(timeout=5.0) == "acquired"


def test_process_exit_releases_held_lock(lock_root: Path, spawned: Any) -> None:
    context, start, _, _ = spawned
    entered, release = context.Event(), context.Event()
    process, _ = start(_hold_lock, str(lock_root), True, entered, release)
    assert entered.wait(5.0)
    process.terminate()
    process.join(timeout=5.0)
    assert not process.is_alive()
    assert not release.is_set(), "holder should exit without voluntarily unlocking"
    with root_lock(lock_root, write=True, timeout=1.0):
        pass


@pytest.mark.parametrize("outer_write,inner_write", [(False, False), (True, False), (True, True)])
def test_same_thread_nested_locks_reuse_outer_ownership(
    lock_root: Path, outer_write: bool, inner_write: bool
) -> None:
    with root_lock(lock_root, write=outer_write, timeout=0):
        with root_lock(lock_root, write=inner_write, timeout=0):
            assert get_config_path() == str(lock_root / "config.toml")
        with root_lock(lock_root, write=inner_write, timeout=0):
            pass


def test_read_to_write_upgrade_is_rejected_and_preserves_read_lock(lock_root: Path) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor, root_lock(lock_root):
        with pytest.raises(BrainError, match="upgrade"), root_lock(lock_root, write=True, timeout=0):
            pytest.fail("upgrade must not succeed")
        future = executor.submit(_thread_attempt, lock_root, True)
        assert future.result(timeout=5.0) == "busy"


def _thread_attempt(root: Path, write: bool) -> str:
    try:
        with root_lock(root, write=write, timeout=0.15):
            return "acquired"
    except RootBusyError:
        return "busy"


@pytest.mark.parametrize("waiter_write", [False, True])
def test_other_thread_cannot_reuse_writer_ownership(lock_root: Path, waiter_write: bool) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        with root_lock(lock_root, write=True):
            future = executor.submit(_thread_attempt, lock_root, waiter_write)
            assert future.result(timeout=5.0) == "busy"
        assert executor.submit(_thread_attempt, lock_root, waiter_write).result(timeout=5.0) == "acquired"


def test_canonical_aliases_share_the_process_lock(lock_root: Path, spawned: Any) -> None:
    _, start, _, _ = spawned
    alias = lock_root / "unused-directory" / ".."
    with root_lock(lock_root, write=True):
        with root_lock(alias, write=True, timeout=0):
            pass
        _, results = start(_attempt_lock, str(alias), True, 0.15)
        assert results.get(timeout=5.0) == "busy"


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_windows_case_aliases_share_the_process_lock(lock_root: Path, spawned: Any) -> None:
    _, start, _, _ = spawned
    with root_lock(lock_root, write=True):
        _, results = start(_attempt_lock, str(lock_root).upper(), True, 0.15)
        assert results.get(timeout=5.0) == "busy"


def test_distinct_roots_can_be_written_in_parallel(lock_root: Path, spawned: Any) -> None:
    _, start, _, _ = spawned
    other_root = lock_root.parent / "other-brain"
    other_root.mkdir()
    with root_lock(lock_root, write=True):
        _, results = start(_attempt_lock, str(other_root), True, 0.15)
        assert results.get(timeout=5.0) == "acquired"


def test_waiting_writer_blocks_later_reader(lock_root: Path, spawned: Any) -> None:
    context, start, processes, queues = spawned
    writer_entered, writer_release, writer_waiting = context.Event(), context.Event(), context.Event()
    writer_results = context.Queue()
    queues.append(writer_results)
    writer = context.Process(
        target=_hold_lock,
        args=(str(lock_root), True, writer_entered, writer_release, writer_results, writer_waiting),
    )
    processes.append(writer)
    try:
        with root_lock(lock_root):
            writer.start()
            assert writer_waiting.wait(5.0), "writer never reached existing-reader wait"
            _, late_reader = start(_attempt_lock, str(lock_root), False, 0.15)
            assert late_reader.get(timeout=5.0) == "busy", "late reader barged ahead of waiting writer"
            assert not writer_entered.is_set()
        assert writer_entered.wait(5.0)
    finally:
        writer_release.set()
    assert writer_results.get(timeout=5.0) == "released"


def test_read_lock_leaves_data_root_contents_unchanged(lock_root: Path) -> None:
    config = lock_root / "config.toml"
    config.write_text("# disposable fixture\n", encoding="utf-8")
    before = {path.relative_to(lock_root): path.read_bytes() for path in lock_root.rglob("*") if path.is_file()}
    before_entries = set(lock_root.rglob("*"))
    with root_lock(lock_root):
        assert set(lock_root.rglob("*")) == before_entries
    assert set(lock_root.rglob("*")) == before_entries
    assert {path.relative_to(lock_root): path.read_bytes() for path in lock_root.rglob("*") if path.is_file()} == before


def test_coordinated_preserves_signature_and_covers_the_complete_call(lock_root: Path) -> None:
    @coordinated(write=True, root_parameter="root")
    def operation(root: Path = lock_root, *, label: str = "result") -> str:
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(_thread_attempt, root, False).result(timeout=5.0) == "busy"
        return label

    assert inspect.signature(operation) == inspect.signature(operation.__wrapped__)
    assert operation() == "result"
    assert operation(root=lock_root, label="custom") == "custom"
    assert _thread_attempt(lock_root, True) == "acquired"


@pytest.mark.parametrize("timeout", [-1.0, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected(lock_root: Path, timeout: float) -> None:
    with (
        pytest.raises(ValueError, match="finite and non-negative"),
        root_lock(lock_root, timeout=timeout),
    ):
        pytest.fail("invalid timeout must not acquire a lock")
