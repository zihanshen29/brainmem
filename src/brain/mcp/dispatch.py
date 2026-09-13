"""Bounded synchronous tool execution outside the MCP transport event loop."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial, wraps
from pathlib import Path as Path  # Resolve the wrapped tools' postponed annotations.
from time import perf_counter
from typing import Any

import anyio

from brain.exceptions import BrainError

LOGGER = logging.getLogger(__name__)

READ_TOOLS = frozenset({
    "brain_status", "brain_ask", "brain_inject", "brain_procedure_list",
    "brain_review_queue", "brain_recent_events",
})


class MCPBusyError(BrainError):
    """The bounded dispatcher cannot admit a tool call in time."""


class ToolDispatcher:
    """One event-loop-owned dispatcher; max_pending includes running work.

    A private task owns each admitted job. Cancelling its caller cancels queued
    work, but leaves already-running synchronous work owning its slot until it
    actually exits. A worker's pipeline context owns the repository lock too.
    """

    def __init__(
        self, *, workers: int = 4, max_pending: int = 16, queue_timeout: float = 10.0,
    ) -> None:
        if workers < 1 or max_pending < workers or not math.isfinite(queue_timeout) or queue_timeout <= 0:
            raise ValueError("workers must be positive, max_pending >= workers, and timeout positive")
        self._slots = asyncio.Semaphore(workers)
        self._threads = anyio.CapacityLimiter(workers)
        self._max_pending = max_pending
        self._queue_timeout = queue_timeout
        self._jobs: set[asyncio.Task[Any]] = set()
        self._closing = False

    async def aclose(self) -> None:
        """Stop admission and wait for actual work before the event loop exits."""
        self._closing = True
        with anyio.CancelScope(shield=True):
            while self._jobs:
                await asyncio.gather(*tuple(self._jobs), return_exceptions=True)

    @asynccontextmanager
    async def lifespan(self, server: Any) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await self.aclose()

    def wrap(self, tool: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(tool)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await self.run(tool, *args, **kwargs)

        return wrapper

    async def run(self, tool: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._closing:
            raise MCPBusyError("BrainMem is shutting down; reconnect to retry")
        if len(self._jobs) >= self._max_pending:
            raise MCPBusyError("BrainMem is busy; retry after current requests finish")
        queued_at = perf_counter()
        started = False
        cancel_requested = False
        caller = asyncio.current_task()

        async def execute() -> Any:
            nonlocal started
            acquired = False
            started_at = queued_at
            outcome = "cancelled"
            try:
                try:
                    async with asyncio.timeout(self._queue_timeout):
                        await self._slots.acquire()
                except TimeoutError as exc:
                    outcome = "queue_timeout"
                    raise MCPBusyError("BrainMem request queue timed out; retry shortly") from exc
                acquired = True
                if cancel_requested or (caller is not None and caller.cancelling()):
                    raise asyncio.CancelledError
                started = True
                started_at = perf_counter()
                outcome = "error"
                result = await anyio.to_thread.run_sync(
                    partial(tool, *args, **kwargs), limiter=self._threads,
                    abandon_on_cancel=False,
                )
                outcome = "ok"
                return result
            finally:
                if acquired:
                    self._slots.release()
                finished_at = perf_counter()
                LOGGER.info(
                    "MCP tool=%s queue_ms=%.1f execution_ms=%.1f outcome=%s",
                    tool.__name__, ((started_at if started else finished_at) - queued_at) * 1000,
                    (finished_at - started_at) * 1000 if started else 0, outcome,
                )

        job = asyncio.create_task(execute())
        self._jobs.add(job)

        def completed(task: asyncio.Task[Any]) -> None:
            self._jobs.discard(task)
            if not task.cancelled():
                task.exception()  # Retrieve errors even after the caller cancelled.

        job.add_done_callback(completed)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            cancel_requested = True
            if not started:
                job.cancel()
                # It cannot start after cancellation. Restore admission now,
                # without waiting for the task's scheduled done callback.
                self._jobs.discard(job)
            raise
