"""Shared test safety: ordinary tests must never reach a real provider."""

from __future__ import annotations

import ipaddress
import socket
from asyncio.proactor_events import BaseProactorEventLoop
from asyncio.selector_events import BaseSelectorEventLoop

import pytest

LOCAL_NAMES = {"", "localhost", "testserver"}


class NetworkBlocked(RuntimeError):
    """A non-live test tried to reach a non-loopback host.

    This deliberately is not an OSError, so gateway code cannot mistake it for a
    retryable network failure and fall back to another real target.
    """


def is_local(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    name = str(host).strip("[]").lower()
    if name in LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def block_external_network(request, monkeypatch):
    """Allow loopback traffic only, unless the test is marked ``live``.

    ``getaddrinfo`` is patched as well as ``connect`` because the Windows Proactor
    event loop connects through ``ConnectEx`` and bypasses ``socket.connect``.
    """
    if request.node.get_closest_marker("live"):
        yield
        return
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not is_local(host):
            raise NetworkBlocked(f"Test attempted DNS lookup for non-loopback host {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not is_local(address[0]):
            raise NetworkBlocked(f"Test attempted to connect to non-loopback host {address[0]!r}")

    def guarded_connect(self, address):
        check(self, address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        check(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    # asyncio resolves IP literals without getaddrinfo and the Proactor loop connects
    # through ConnectEx, so the event-loop entry points are guarded too.
    for loop_class in (BaseProactorEventLoop, BaseSelectorEventLoop):
        real_sock_connect = loop_class.sock_connect

        async def guarded_sock_connect(self, sock, address, _real=real_sock_connect):
            check(sock, address)
            return await _real(self, sock, address)

        monkeypatch.setattr(loop_class, "sock_connect", guarded_sock_connect)
    yield
