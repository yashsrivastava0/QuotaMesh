"""The autouse guard in conftest.py keeps ordinary tests away from real providers."""

import socket
import threading

import httpx
import pytest
from conftest import NetworkBlocked, is_local


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("testserver", True),
        (None, True),
        ("api.openai.com", False),
        ("generativelanguage.googleapis.com", False),
        ("1.1.1.1", False),
        ("10.0.0.1", False),
    ],
)
def test_loopback_classification(host, local):
    assert is_local(host) is local


def test_sync_connections_to_remote_hosts_are_blocked():
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("api.openai.com", 443)
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("1.1.1.1", 443), timeout=1)


def contains_blocked(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, NetworkBlocked):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(contains_blocked(inner) for inner in exc.exceptions)
    return contains_blocked(exc.__cause__)


@pytest.mark.parametrize("url", ["https://api.openai.com/v1/models", "https://1.1.1.1/"])
async def test_async_http_to_provider_is_blocked_not_retried(url):
    # IP literals skip DNS and the Proactor loop connects via ConnectEx; anyio may wrap
    # the error in an ExceptionGroup. Either way it must not become an httpx error.
    async with httpx.AsyncClient() as client:
        with pytest.raises((NetworkBlocked, BaseExceptionGroup)) as info:
            await client.get(url)
    assert contains_blocked(info.value)
    assert not isinstance(info.value, httpx.HTTPError)


def test_loopback_server_still_reachable():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted = []
    thread = threading.Thread(target=lambda: accepted.append(server.accept()[0]))
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
        thread.join(5)
        assert accepted
    finally:
        for conn in accepted:
            conn.close()
        server.close()
