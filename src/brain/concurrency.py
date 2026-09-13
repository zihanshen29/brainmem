"""Coordinate complete BrainMem operations across threads and local processes.

Locks live outside the data root, so read-only operations do not alter a brain.
Every process accessing a root must use the same lock directory. These locks
coordinate cooperating BrainMem processes on one machine, not filesystem edits
made by other programs or copies of the data root on different machines.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

import portalocker

from brain.config_context import configured_path
from brain.exceptions import BrainError

DEFAULT_LOCK_TIMEOUT = 10.0
LOCK_CHECK_INTERVAL = 0.025
_local = threading.local()
P = ParamSpec("P")
R = TypeVar("R")


class RootBusyError(BrainError):
    """Another operation held a conflicting root lock past the wait budget."""


def _held_roots() -> dict[str, bool]:
    # A fork must not inherit the parent's permission to bypass acquisition.
    if getattr(_local, "pid", None) != os.getpid():
        _local.pid = os.getpid()
        _local.roots = {}
    return _local.roots


def _lock_directory() -> Path:
    configured = os.environ.get("BRAINMEM_LOCK_DIR")
    directory = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(tempfile.gettempdir()) / "brainmem-locks"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _file_lock(path: Path, *, shared: bool, deadline: float) -> portalocker.Lock:
    flags = portalocker.LOCK_SH if shared else portalocker.LOCK_EX
    return portalocker.Lock(
        path,
        mode="a+b",
        timeout=max(0.0, deadline - time.monotonic()),
        check_interval=LOCK_CHECK_INTERVAL,
        flags=flags | portalocker.LOCK_NB,
    )


@contextmanager
def root_lock(
    root: Path | str,
    *,
    write: bool = False,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> Iterator[None]:
    """Hold a shared read or exclusive write lock for a complete operation.

    Nested calls on the same thread reuse the outer lock. A read-to-write
    upgrade is rejected instead of deadlocking. The synchronous caller owns the
    lock until its function exits, including when an MCP client disconnects.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("lock timeout must be finite and non-negative")
    canonical_root = Path(root).expanduser().resolve()
    identity = os.path.normcase(str(canonical_root))
    held = _held_roots()
    if identity in held:
        if write and not held[identity]:
            raise BrainError("Cannot upgrade a read operation to a write operation")
        yield
        return

    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    directory = _lock_directory()
    deadline = time.monotonic() + timeout
    gate = _file_lock(directory / f"{key}.gate", shared=False, deadline=deadline)
    data: portalocker.Lock | None = None
    try:
        # Readers release the gate after taking a shared lock. A waiting writer
        # holds it while existing readers drain, so later readers cannot barge.
        with gate:
            data = _file_lock(directory / f"{key}.lock", shared=not write, deadline=deadline)
            data.acquire()
    except portalocker.LockException as exc:
        if data is not None:
            data.release()
        raise RootBusyError(
            "Brain root is busy with another operation; retry after it finishes"
        ) from exc

    try:
        held[identity] = write
        with configured_path(canonical_root / "config.toml"):
            yield
    finally:
        held.pop(identity, None)
        if data is not None:
            data.release()


def coordinated(
    *, write: bool = False, root_parameter: str = "brain_root"
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Protect a synchronous pipeline entry point without changing its schema."""
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)
        if root_parameter not in signature.parameters:
            raise TypeError(f"{function.__name__} has no {root_parameter} parameter")

        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            with root_lock(bound.arguments[root_parameter], write=write):
                return function(*args, **kwargs)

        return wrapper

    return decorate
