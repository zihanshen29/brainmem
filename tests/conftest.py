from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli.init import init_brain


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("BRAINMEM_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Providers must be mocked; allow only asyncio's local self-pipe IPC."""
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    original_lookup = socket.getaddrinfo
    original_sendto = socket.socket.sendto
    guard = str(Path(__file__).parent / "offline")
    previous = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", guard + (os.pathsep + previous if previous else ""))

    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("Network is disabled in BrainMem tests")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)

    def connect_ex(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("Network is disabled in BrainMem tests")
        return original_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)

    def getaddrinfo(host, *args, **kwargs):
        if host not in {None, "localhost", b"localhost"}:
            try:
                ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
            except ValueError as exc:
                raise AssertionError("External DNS disabled in BrainMem tests") from exc
        return original_lookup(host, *args, **kwargs)

    def sendto(sock, data, *args):
        address = args[-1] if args else None
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("Network is disabled in BrainMem tests")
        return original_sendto(sock, data, *args)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "sendto", sendto)


@pytest.fixture()
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    return root


@pytest.fixture()
def fake_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Satisfy preflight while making accidental real provider calls fail closed."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")

    def reject_real_provider_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider call must be mocked in tests using fake_provider_key")

    monkeypatch.setattr("brain.llm.client._extract_impl", reject_real_provider_call)


@pytest.fixture()
def cli_runner() -> CliRunner:
    return CliRunner()
