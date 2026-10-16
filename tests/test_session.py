"""Login links, port-scoped session cookies and constant-time local authorization."""

import socket
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from quotamesh import security
from quotamesh.app import create_app
from quotamesh.cli import app as cli
from quotamesh.cli import run_server
from quotamesh.store import Store


def client_for(app, base_url="http://testserver"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


def token_of(url):
    return parse_qs(urlsplit(url).query)["token"][0]


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, allow_test_host=True)


async def test_cookie_name_is_scoped_to_the_port_with_strict_attributes(app):
    async with client_for(app, "http://127.0.0.1:9123") as client:
        response = await client.get("/bootstrap", params={"token": app.state.bootstrap_token})
        assert response.status_code == 303 and response.headers["location"] == "/"
        cookie = response.headers["set-cookie"].lower()
        assert cookie.startswith("qm_session_9123=")
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
        assert (await client.get("/")).status_code == 200
    async with client_for(app, "http://127.0.0.1:9124") as other:
        other.cookies.set("qm_session_9123", app.state.session_token)
        other.cookies.set("qm_session", app.state.session_token)
        assert (await other.get("/")).status_code == 401  # Another port's cookie name.
    async with client_for(app) as plain:
        plain.cookies.set("qm_session", app.state.session_token)
        assert (await plain.get("/")).status_code == 200  # No port: legacy name.


async def test_signed_in_bootstrap_redirects_without_consuming(app):
    printed = app.state.bootstrap_token
    spare = security.mint_bootstrap_token(app)
    async with client_for(app) as client:
        await client.get("/bootstrap", params={"token": printed})
        assert app.state.bootstrap_token is None
        again = await client.get("/bootstrap", params={"token": spare})
        assert again.status_code == 303 and "set-cookie" not in again.headers
    async with client_for(app) as second_browser:
        response = await second_browser.get("/bootstrap", params={"token": spare})
        assert response.status_code == 303 and "set-cookie" in response.headers


async def test_used_or_invalid_links_and_locked_pages_explain_the_way_back(app):
    async with client_for(app) as client:
        await client.get("/bootstrap", params={"token": app.state.bootstrap_token})
    async with client_for(app) as stranger:
        for token in ("used", "", app.state.session_token):
            response = await stranger.get("/bootstrap", params={"token": token})
            assert response.status_code == 403
            assert response.headers["content-type"].startswith("text/html")
            assert "already used or has expired" in response.text
            assert "<code>quotamesh open</code>" in response.text
            assert response.headers["cache-control"] == "no-store"
        locked = await stranger.get("/")
        assert locked.status_code == 401 and "quotamesh open" in locked.text
        assert "<script" not in locked.text and "style=" not in locked.text


async def test_non_ascii_credentials_are_rejected_not_errors(app):
    async with client_for(app) as client:
        for value in ("Bearer \xe9t\xe9", "Bearer ÿ" + app.state.local_key):
            response = await client.get(
                "/api/status", headers={"Authorization": value.encode("latin-1")}
            )
            assert response.status_code == 401
        cookie = ("qm_session=séssion; qm_session_1=" + app.state.session_token).encode("latin-1")
        assert (await client.get("/", headers={"Cookie": cookie})).status_code == 401
        for raw in ("%C3%A9", "%ED%A0%80", "%FF"):
            assert (await client.get("/bootstrap?token=" + raw)).status_code == 403
    assert not security.secret_matches("\ud800", "token")
    assert security.secret_matches("té", "té")


async def test_session_link_is_bearer_only_single_use_and_expires(app, monkeypatch):
    auth = {"Authorization": "Bearer " + app.state.local_key}
    async with client_for(app, "http://127.0.0.1:8787") as cli_client:
        response = await cli_client.post("/api/session/link", headers=auth)
        assert response.status_code == 201
        data = response.json()
        assert data["expires_in"] == 120 and response.headers["cache-control"] == "no-store"
        assert data["url"].startswith("http://127.0.0.1:8787/bootstrap?token=")
        assert (await cli_client.post("/api/session/link")).status_code == 401
        expiring = token_of(
            (await cli_client.post("/api/session/link", headers=auth)).json()["url"]
        )
    async with client_for(app) as browser:
        await browser.get("/bootstrap", params={"token": app.state.bootstrap_token})
        # A browser session alone cannot mint links.
        assert (await browser.post("/api/session/link")).status_code == 401
    async with client_for(app) as fresh:
        assert (
            await fresh.get("/bootstrap", params={"token": token_of(data["url"])})
        ).status_code == 303
    async with client_for(app) as reused:
        assert (
            await reused.get("/bootstrap", params={"token": token_of(data["url"])})
        ).status_code == 403
    later = time.monotonic() + 121
    monkeypatch.setattr(security.time, "monotonic", lambda: later)
    async with client_for(app) as late:
        assert (await late.get("/bootstrap", params={"token": expiring})).status_code == 403


def test_pending_links_are_bounded_and_the_printed_link_survives(app):
    printed = app.state.bootstrap_token
    for _ in range(security.MAX_PENDING_LINKS + 10):
        security.mint_bootstrap_token(app)
    assert len(app.state.bootstrap_tokens) == security.MAX_PENDING_LINKS + 1
    assert app.state.bootstrap_tokens[printed] is None


def test_auto_opened_browser_gets_its_own_token(app, monkeypatch):
    opened = []

    class FakeServer:
        def __init__(self, config):
            self.started = self.should_exit = False

        def run(self, sockets=None):
            self.started = True
            for _ in range(200):
                if opened:
                    return
                time.sleep(0.02)

    monkeypatch.setattr("quotamesh.cli.uvicorn.Server", FakeServer)
    monkeypatch.setattr("quotamesh.cli.webbrowser.open", opened.append)
    printed = app.state.bootstrap_token
    run_server(app, 8787, True)
    assert opened and token_of(opened[0]) != printed
    assert token_of(opened[0]) in app.state.bootstrap_tokens
    assert app.state.bootstrap_tokens[printed] is None  # The printed link stays usable.


def cli_against(app, monkeypatch):
    auth = {"Authorization": "Bearer " + app.state.local_key}
    monkeypatch.setattr(
        "quotamesh.cli.local_client",
        lambda port: TestClient(app, base_url=f"http://127.0.0.1:{port}", headers=auth),
    )


def test_cli_open_prints_or_opens_a_fresh_link(app, monkeypatch):
    cli_against(app, monkeypatch)
    printed = CliRunner().invoke(cli, ["open", "--print", "--port", "8899"])
    assert printed.exit_code == 0, printed.output
    url = printed.stdout.strip()
    assert url.startswith("http://127.0.0.1:8899/bootstrap?token=")
    assert token_of(url) in app.state.bootstrap_tokens
    opened = []
    monkeypatch.setattr("quotamesh.cli.webbrowser.open", lambda link: opened.append(link) or True)
    result = CliRunner().invoke(cli, ["open"])
    assert result.exit_code == 0 and "Opened" in result.stdout
    assert opened and token_of(opened[0]) != token_of(url)
    assert token_of(opened[0]) not in result.stdout


def test_cli_open_explains_a_stopped_server(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAMESH_DATA_DIR", str(tmp_path))
    Store(tmp_path).gateway_key()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = CliRunner().invoke(cli, ["open", "--port", str(port)])
    assert result.exit_code == 1
    assert f"QuotaMesh is not running on port {port}" in result.stdout
