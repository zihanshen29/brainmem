"""Offline guard inherited by test Python subprocesses; loopback is local IPC."""

import ipaddress
import os
import socket

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo
_sendto = socket.socket.sendto


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


def getaddrinfo(host, *args, **kwargs):
    if host not in {None, "localhost", b"localhost"}:
        try:
            ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
        except ValueError as exc:
            raise AssertionError("External DNS disabled in BrainMem tests") from exc
    return _getaddrinfo(host, *args, **kwargs)


def sendto(self, data, *args):
    if not args or not _local(args[-1]):
        raise AssertionError("External network disabled in BrainMem tests")
    return _sendto(self, data, *args)


socket.getaddrinfo = getaddrinfo
socket.socket.sendto = sendto
for name in tuple(os.environ):
    if name.endswith("_API_KEY"):
        os.environ.pop(name, None)
