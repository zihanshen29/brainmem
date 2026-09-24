"""Offline guard inherited by test Python subprocesses; loopback is local IPC."""

import os
import socket

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _local(address):
    return isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}


def connect(self, address):
    if not _local(address):
        raise AssertionError("External network disabled in BrainMem tests")
    return _connect(self, address)


def connect_ex(self, address):
    if not _local(address):
        raise AssertionError("External network disabled in BrainMem tests")
    return _connect_ex(self, address)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
for name in tuple(os.environ):
    if name.endswith("_API_KEY"):
        os.environ.pop(name, None)
