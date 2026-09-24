import socket
import subprocess
import sys

import pytest


def test_external_dns_and_udp_are_blocked_before_any_request():
    with pytest.raises(AssertionError, match='DNS'):
        socket.getaddrinfo('example.invalid', 443)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock, pytest.raises(
        AssertionError, match=r'Network|network'
    ):
        sock.sendto(b'no network', ('198.51.100.1', 53))


def test_python_child_inherits_dns_guard():
    script = "import socket\ntry:\n socket.getaddrinfo('example.invalid',443)\nexcept AssertionError:\n print('blocked')\nelse:\n raise SystemExit('guard missing')"
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'blocked'
