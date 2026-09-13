from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from brain.mcp import server as stdio_server
from brain.mcp import server_http, tools
from brain.mcp.http_config import HttpConfig


async def _wait_event(event: threading.Event) -> None:
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.005)


def _build_server(transport: str, root: Path, **kwargs: Any) -> Any:
    if transport == "stdio":
        return stdio_server.build_server(**kwargs)
    return server_http.build_server(
        HttpConfig(
            brain_root=root,
            enabled_tools=frozenset({"brain_status", "brain_procedure_list"}),
        ),
        **kwargs,
    )


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_slow_read_does_not_block_fast_read(
    transport: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = threading.Event(), threading.Event()

    def slow_read(brain_root: str | None = None) -> dict[str, bool]:
        started.set()
        return {"released": release.wait(2)}

    def fast_read(brain_root: str | None = None) -> dict[str, bool]:
        return {"fast": True}

    slow_read.__name__ = "brain_status"
    fast_read.__name__ = "brain_procedure_list"
    monkeypatch.setattr(tools, "brain_status", slow_read)
    monkeypatch.setattr(tools, "brain_procedure_list", fast_read)
    server = _build_server(transport, tmp_path)

    async def exercise() -> None:
        first = asyncio.create_task(server.call_tool("brain_status", {}))
        try:
            await _wait_event(started)
            assert not first.done(), "synchronous tool blocked the event loop"
            result = await asyncio.wait_for(
                server.call_tool("brain_procedure_list", {}), timeout=0.5
            )
            assert json.loads(result[0][0].text) == {"fast": True}
            assert not first.done()
        finally:
            release.set()
            result = await first
        assert json.loads(result[0][0].text) == {"released": True}

    asyncio.run(exercise())


def test_dispatcher_bounds_running_and_admitted_work() -> None:
    from brain.mcp.dispatch import MCPBusyError, ToolDispatcher

    dispatcher = ToolDispatcher(workers=4, max_pending=6, queue_timeout=1)
    all_workers_started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = 0
    executed: list[int] = []

    def slow_read(number: int) -> int:
        nonlocal active, peak
        with lock:
            executed.append(number)
            active += 1
            peak = max(peak, active)
            if active == 4:
                all_workers_started.set()
        try:
            assert release.wait(3), "test did not release the workers"
            return number
        finally:
            with lock:
                active -= 1

    async def exercise() -> None:
        pending = [asyncio.create_task(dispatcher.run(slow_read, n)) for n in range(6)]
        try:
            await _wait_event(all_workers_started)
            with pytest.raises(MCPBusyError):
                await asyncio.wait_for(dispatcher.run(slow_read, 99), timeout=0.5)
            assert len(executed) == peak == 4
            assert not any(task.done() for task in pending)
        finally:
            release.set()
            results = await asyncio.gather(*pending)
        assert results == list(range(6))
        assert sorted(executed) == list(range(6))
        assert peak == 4
        assert await dispatcher.run(lambda: "capacity restored") == "capacity restored"

    asyncio.run(exercise())


def test_queue_timeout_does_not_execute_waiting_work_or_stop_running_work() -> None:
    from brain.mcp.dispatch import MCPBusyError, ToolDispatcher

    dispatcher = ToolDispatcher(workers=1, max_pending=2, queue_timeout=0.05)
    started, release, queued_ran = (threading.Event() for _ in range(3))

    def slow_read() -> str:
        started.set()
        assert release.wait(3)
        return "finished"

    async def exercise() -> None:
        first = asyncio.create_task(dispatcher.run(slow_read))
        try:
            await _wait_event(started)
            with pytest.raises(MCPBusyError):
                await asyncio.wait_for(dispatcher.run(queued_ran.set), timeout=0.5)
            assert not queued_ran.is_set()
            assert not first.done()
        finally:
            release.set()
            assert await first == "finished"
        assert await dispatcher.run(lambda: "next") == "next"
        assert not queued_ran.is_set()

    asyncio.run(exercise())


def test_cancelling_running_caller_keeps_capacity_until_sync_work_finishes() -> None:
    from brain.mcp.dispatch import MCPBusyError, ToolDispatcher

    dispatcher = ToolDispatcher(workers=1, max_pending=1, queue_timeout=1)
    started, release, finished, rejected_ran = (threading.Event() for _ in range(4))

    def slow_read() -> None:
        started.set()
        try:
            assert release.wait(3)
        finally:
            finished.set()

    async def exercise() -> None:
        first = asyncio.create_task(dispatcher.run(slow_read))
        try:
            await _wait_event(started)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, timeout=0.5)
            assert not finished.is_set(), "cancellation waited for sync work to finish"
            with pytest.raises(MCPBusyError):
                await asyncio.wait_for(dispatcher.run(rejected_ran.set), timeout=0.5)
            assert not rejected_ran.is_set()
        finally:
            release.set()
            await _wait_event(finished)
            await asyncio.gather(first, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("queue_checkpoints", [0, 3])
def test_cancelling_queued_caller_never_executes_it_and_restores_admission(
    queue_checkpoints: int,
) -> None:
    from brain.mcp.dispatch import ToolDispatcher

    dispatcher = ToolDispatcher(workers=1, max_pending=2, queue_timeout=1)
    started, release, cancelled_ran = (threading.Event() for _ in range(3))

    def slow_read() -> str:
        started.set()
        assert release.wait(3)
        return "finished"

    async def exercise() -> None:
        enqueued = asyncio.Event()

        async def queued_read() -> None:
            enqueued.set()
            await dispatcher.run(cancelled_ran.set)

        first = asyncio.create_task(dispatcher.run(slow_read))
        queued = replacement = None
        try:
            await _wait_event(started)
            queued = asyncio.create_task(queued_read())
            await enqueued.wait()
            # Cover cancellation before the private job starts and after it waits
            # on the occupied worker slot, without depending on elapsed time.
            for _ in range(queue_checkpoints):
                await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(queued, timeout=0.5)
            replacement = asyncio.create_task(dispatcher.run(lambda: "replacement"))
            await asyncio.sleep(0)
            assert not replacement.done()
            assert not cancelled_ran.is_set()
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (first, queued, replacement) if task is not None),
                return_exceptions=True,
            )
        assert first.result() == "finished"
        assert replacement is not None and replacement.result() == "replacement"
        assert not cancelled_ran.is_set()

    asyncio.run(exercise())


def test_sync_error_restores_capacity_and_preserves_exception() -> None:
    from brain.mcp.dispatch import ToolDispatcher

    dispatcher = ToolDispatcher(workers=1, max_pending=1, queue_timeout=1)

    def broken_read() -> None:
        raise ValueError("fixture failure")

    async def exercise() -> None:
        with pytest.raises(ValueError, match="fixture failure"):
            await dispatcher.run(broken_read)
        assert await dispatcher.run(lambda: "next") == "next"

    asyncio.run(exercise())


def test_cancellation_as_slot_becomes_available_never_executes_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain.mcp.dispatch import ToolDispatcher

    dispatcher = ToolDispatcher(workers=1, max_pending=1, queue_timeout=1)
    executed = threading.Event()

    async def exercise() -> None:
        # Force the real acquire/cancel race: Python 3.11 asyncio.wait_for can
        # swallow cancellation when the acquisition future is already done.
        acquire = dispatcher._slots.acquire

        async def acquire_then_cancel() -> bool:
            acquired = await acquire()
            caller.cancel()
            return acquired

        monkeypatch.setattr(dispatcher._slots, "acquire", acquire_then_cancel)
        caller = asyncio.create_task(dispatcher.run(executed.set))
        with pytest.raises(asyncio.CancelledError):
            await caller
        monkeypatch.setattr(dispatcher._slots, "acquire", acquire)
        assert await asyncio.wait_for(dispatcher.run(lambda: "next"), timeout=0.5) == "next"
        assert not executed.is_set()

    asyncio.run(exercise())


def test_orderly_close_drains_cancelled_running_job_and_refuses_new_work() -> None:
    from brain.mcp.dispatch import MCPBusyError, ToolDispatcher

    dispatcher = ToolDispatcher(workers=1, max_pending=1)
    started, release, finished = (threading.Event() for _ in range(3))

    def slow_write() -> None:
        started.set()
        assert release.wait(3)
        finished.set()

    async def exercise() -> None:
        caller = asyncio.create_task(dispatcher.run(slow_write))
        closing = None
        try:
            await _wait_event(started)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            closing = asyncio.create_task(dispatcher.aclose())
            await asyncio.sleep(0)
            assert not closing.done()
            assert not finished.is_set()
            with pytest.raises(MCPBusyError):
                await dispatcher.run(lambda: "must not be admitted")
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, timeout=1)
            else:
                await asyncio.gather(caller, return_exceptions=True)
        assert finished.is_set()
        with pytest.raises(MCPBusyError):
            await dispatcher.run(lambda: "closed")

    asyncio.run(exercise())


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_mcp_anyio_cancel_scope_keeps_running_work_capacity(
    transport: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    from brain.mcp.dispatch import ToolDispatcher

    started, release, finished = (threading.Event() for _ in range(3))

    def slow_read(brain_root: str | None = None) -> dict[str, bool]:
        started.set()
        try:
            return {"released": release.wait(3)}
        finally:
            finished.set()

    slow_read.__name__ = "brain_status"
    monkeypatch.setattr(tools, "brain_status", slow_read)
    server = _build_server(
        transport, tmp_path, dispatcher=ToolDispatcher(workers=1, max_pending=1)
    )

    async def exercise() -> None:
        scopes: list[anyio.CancelScope] = []
        caller_done = anyio.Event()

        async def request() -> None:
            # MCP RequestResponder uses this AnyIO cancellation mechanism.
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await server.call_tool("brain_status", {})
            caller_done.set()

        async with anyio.create_task_group() as group:
            group.start_soon(request)
            try:
                await _wait_event(started)
                scopes[0].cancel()
                with anyio.fail_after(0.5):
                    await caller_done.wait()
                assert not finished.is_set()
                if transport == "stdio":
                    with pytest.raises(ToolError):
                        await server.call_tool("brain_status", {})
                else:
                    result = await server.call_tool("brain_status", {})
                    assert json.loads(result[0][0].text)["error"]["code"] == "busy"
            finally:
                release.set()
                await _wait_event(finished)

    anyio.run(exercise)


def test_async_wrapper_preserves_tool_signature_and_docstring() -> None:
    from brain.mcp.dispatch import ToolDispatcher

    def read_tool(query: str, *, top: int = 5, brain_root: str | None = None) -> dict:
        """Local fixture read with a keyword-only limit."""
        return {"query": query, "top": top, "brain_root": brain_root}

    wrapped = ToolDispatcher().wrap(read_tool)
    assert inspect.iscoroutinefunction(wrapped)
    assert inspect.signature(wrapped) == inspect.signature(read_tool)
    assert wrapped.__annotations__ == read_tool.__annotations__
    assert wrapped.__name__ == read_tool.__name__
    assert wrapped.__doc__ == read_tool.__doc__
    assert asyncio.run(wrapped("fixture", top=2)) == {
        "query": "fixture", "top": 2, "brain_root": None
    }


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_registered_async_tool_preserves_schema_and_argument_validation(
    transport: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read_tool(count: int = 1, brain_root: str | None = None) -> dict[str, int]:
        return {"count": count}

    read_tool.__name__ = "brain_status"
    monkeypatch.setattr(tools, "brain_status", read_tool)
    server = _build_server(transport, tmp_path)
    registered = server._tool_manager.get_tool("brain_status")
    assert registered.parameters["properties"]["count"]["type"] == "integer"
    assert ("brain_root" in registered.parameters["properties"]) == (transport == "stdio")
    result = asyncio.run(server.call_tool("brain_status", {"count": "3"}))
    assert json.loads(result[0][0].text) == {"count": 3}


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_missing_optional_vector_dependency_does_not_prevent_mcp_startup(
    transport: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brain.db import connection

    def missing_optional_module(name: str) -> None:
        assert name == "sqlite_vec"
        raise ModuleNotFoundError("optional vector support is absent")

    monkeypatch.setattr(
        connection, "importlib", SimpleNamespace(import_module=missing_optional_module)
    )
    server = _build_server(transport, tmp_path)
    assert server._tool_manager.get_tool("brain_status") is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native loader/stdin lock regression")
def test_stdio_startup_prepares_native_vector_import_before_blocking_stdin(
    tmp_path: Path,
) -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("sqlite_vec")
    ready = tmp_path / "native-import-finished"
    script = """
import sys, threading, time
from pathlib import Path
from brain.mcp.server import build_server
build_server()
reader = threading.Thread(target=lambda: sys.stdin.buffer.read1(1), daemon=True)
reader.start()
time.sleep(0.05)
import sqlite_vec
Path(sys.argv[1]).touch()
reader.join()
"""
    with (tmp_path / "native-import.stderr.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(ready)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "native import waited for the live stdin reader to close"
        finally:
            assert process.stdin is not None
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
        assert process.returncode == 0


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_transport_uses_injected_dispatcher_and_exposes_overload(
    transport: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    from brain.mcp.dispatch import ToolDispatcher

    dispatcher = ToolDispatcher(workers=1, max_pending=1, queue_timeout=1)
    started, release = threading.Event(), threading.Event()
    calls = 0

    def slow_read(brain_root: str | None = None) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        started.set()
        return {"released": release.wait(3)}

    slow_read.__name__ = "brain_status"
    monkeypatch.setattr(tools, "brain_status", slow_read)
    server = _build_server(transport, tmp_path, dispatcher=dispatcher)

    async def exercise() -> None:
        first = asyncio.create_task(server.call_tool("brain_status", {}))
        try:
            await _wait_event(started)
            if transport == "stdio":
                with pytest.raises(ToolError):
                    await asyncio.wait_for(server.call_tool("brain_status", {}), 0.5)
            else:
                result = await asyncio.wait_for(server.call_tool("brain_status", {}), 0.5)
                payload = json.loads(result[0][0].text)
                assert payload["error"]["code"] == "busy"
                assert str(tmp_path) not in result[0][0].text
            assert calls == 1
            assert not first.done()
        finally:
            release.set()
            result = await first
        assert json.loads(result[0][0].text) == {"released": True}

    asyncio.run(exercise())
