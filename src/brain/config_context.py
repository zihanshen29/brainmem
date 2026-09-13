"""Request-local configuration selection without mutating process environment."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_config_path: ContextVar[str | None] = ContextVar("brain_config_path", default=None)


def get_config_path() -> str | None:
    return _config_path.get() or os.environ.get("BRAIN_CONFIG")


@contextmanager
def configured_path(path: Path) -> Iterator[None]:
    token = _config_path.set(str(path))
    try:
        yield
    finally:
        _config_path.reset(token)
