"""Explain/Connect acceptance and diagnostics safety, using fake upstreams only."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_phase_two import call, setup
from typer.testing import CliRunner

from quotamesh.activity import ActivityFeed
from quotamesh.cli import app as cli
from quotamesh.connect import integration_snippets
from quotamesh.store import Store


async def check(client, auth, **values):
    return await client.post(
        "/api/doctor",
        headers=auth,
        json={
            "credential_id": 1,
            "mode": "models",
            **values,
        },
    )


async def test_explain_is_passive_and_matches_live_selection_and_caps(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(200, json={"choices": []})

    app, client, auth = await setup(
        tmp_path,
        handler,
        keys=[
            {"plan_type": "PAID"},
            {},
            {"provider_id": "openai"},
        ],
    )
    async with client:
        response = await client.get("/api/explain", headers=auth)
        data = response.json()
        assert not calls
        assert data["decisions"][0]["skip_reason"] == "paid_blocked"
        assert data["selected"]["credential_id"] == 2
        assert data["unallocated"][0]["credential_id"] == 3
        assert data["caps"]["daily"]["headroom_usd"] is None
        assert (await call(client, auth)).status_code == 200
        assert calls == ["Bearer fake-2"]
        assert (await client.get("/api/explain?profile=missing", headers=auth)).status_code == 404
        assert "secret_value" not in response.text and "fake-1" not in response.text
        app.state.accounting_failed = True
        data = (await client.get("/api/explain", headers=auth)).json()
        assert data["decisions"][0]["skip_reason"] == "accounting_unavailable"


async def test_explain_recovery_and_unknown_headroom(tmp_path):
    _, client, auth = await setup(
        tmp_path,
        lambda r: httpx.Response(
            429, json={"error": {"message": "rate limit"}}, headers={"Retry-After": "5"}
        ),
        keys=[{}],
        policy={"paid_daily_cap_usd": 2},
    )
    async with client:
        await call(client, auth)
        data = (await client.get("/api/explain", headers=auth)).json()
        assert data["earliest_recovery_at"] and data["selected"] is None
        assert data["caps"]["daily"]["headroom_usd"] == 2


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"object": "list", "data": [{"id": "model"}, {"id": "model"}]}, "listing_ok"),
        (200, {"choices": []}, "protocol_error"),
        (200, {"data": [{"id": "fake-1"}]}, "protocol_error"),
        (200, {"data": [{"id": "<script>alert(1)</script>"}]}, "protocol_error"),
        (401, {"error": {"message": "invalid api key fake-1"}}, "auth"),
        (404, {"error": {}}, "listing_unsupported"),
        (302, {}, "redirect_blocked"),
        (429, {"error": {"message": "rate limit fake-1"}}, "rate_limit"),
    ],
)
async def test_doctor_listing_classifies_without_spending_or_leaking(
    tmp_path, status, body, expected
):
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(status, json=body, headers={"Location": "https://elsewhere.invalid"})

    app, client, auth = await setup(tmp_path, handler, keys=[{"plan_type": "PAID"}])
    async with client:
        response = await check(client, auth)
        assert response.status_code == 200
        result = response.json()
        assert result["outcome"] == expected
        assert len(calls) == 1 and calls[0][0] == "GET"
        assert not app.state.store.recent_attempts()
        checks = (await client.get("/api/doctor", headers=auth)).text
        assert "fake-1" not in checks and "secret_value" not in checks
        assert app.state.store.routing_snapshot(datetime.now(UTC))[3] == {}
        if status == 401:
            assert (
                app.state.store.safe_routing(datetime.now(UTC))["credentials"][0]["status"]
                == "INVALID"
            )


async def test_doctor_bounded_body_and_network_failure(tmp_path):
    app, client, auth = await setup(
        tmp_path, lambda r: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
    )
    async with client:
        assert (await check(client, auth)).json()["outcome"] == "protocol_error"
        app.state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: (_ for _ in ()).throw(httpx.ConnectError("private error fake-1"))
            )
        )
        assert (await check(client, auth)).json()["outcome"] == "network_error"


async def test_generation_requires_consent_obeys_paid_rules_and_never_falls_back(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(429, json={"error": {"message": "rate limit fake-1"}})

    app, client, auth = await setup(tmp_path, handler, keys=[{"plan_type": "PAID"}, {}])
    async with client:
        assert (await check(client, auth, mode="generation", position=1)).status_code == 400
        blocked = (await check(client, auth, mode="generation", position=1, consent=True)).json()
        assert blocked["outcome"] == "paid_blocked" and not calls
        response = await check(
            client, auth, credential_id=2, mode="generation", position=1, consent=True
        )
        assert response.status_code == 200 and response.json()["outcome"] == "rate_limit"
        assert calls == ["Bearer fake-2"]
        assert len(app.state.store.recent_attempts()) == 1
        assert "fake-1" not in response.text
        assert app.state.store.usage_rows()[0]["routed_requests"] == 1


async def test_generation_paid_cost_and_cap_are_durable(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [], "usage": {"cost_usd": 0.6}})

    app, client, auth = await setup(
        tmp_path,
        handler,
        keys=[{"plan_type": "PAID"}],
        targets=[{"provider_id": "custom", "model": "model", "input_price": 1, "output_price": 1}],
        policy={"allow_paid": True, "paid_daily_cap_usd": 0.5},
    )
    async with client:
        assert (await check(client, auth, mode="generation", position=1, consent=True)).json()[
            "outcome"
        ] == "ok"
        assert (await check(client, auth, mode="generation", position=1, consent=True)).json()[
            "outcome"
        ] == "cap_reached"
        assert len(calls) == 1
        assert Store(tmp_path).routing_snapshot(datetime.now(UTC))[4]["daily"] == 0.6
        assert app.state.store.checks(datetime.now(UTC))[0]["models"] == []


async def test_probe_rotation_race_and_cancellation_release_lock(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"data": [{"id": "old-model"}]})

    app, client, auth = await setup(tmp_path, handler)
    async with client:
        task = asyncio.create_task(check(client, auth))
        await entered.wait()
        assert (await check(client, auth)).status_code == 409
        await client.patch("/api/credentials/1", headers=auth, json={"secret_value": "new-secret"})
        release.set()
        assert (await task).json()["saved"] is False
        assert not app.state.store.checks(datetime.now(UTC))
        entered.clear()
        release.clear()
        task = asyncio.create_task(check(client, auth))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not app.state.doctor_running


async def test_discovery_stale_rotation_and_deletion(tmp_path):
    app, client, auth = await setup(
        tmp_path, lambda r: httpx.Response(200, json={"data": [{"id": "model"}]})
    )
    async with client:
        await check(client, auth)
        assert app.state.store.checks(datetime.now(UTC) + timedelta(days=2))[0]["stale"]
        await client.patch("/api/credentials/1", headers=auth, json={"label": "renamed"})
        assert app.state.store.checks(datetime.now(UTC))
        await client.patch("/api/credentials/1", headers=auth, json={"secret_value": "rotated"})
        assert not app.state.store.checks(datetime.now(UTC))
        await check(client, auth)
        assert (await client.delete("/api/credentials/1", headers=auth)).status_code == 200
        assert not app.state.store.checks(datetime.now(UTC))


async def test_environment_is_server_only_reference_idempotent_and_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-private-value")
    monkeypatch.setenv("UNRELATED_SECRET", "never-list-this")
    app, client, auth = await setup(
        tmp_path, lambda r: pytest.fail("Import must not call provider")
    )
    async with client:
        preview = await client.get("/api/environment", headers=auth)
        assert (
            "environment-private-value" not in preview.text
            and "UNRELATED_SECRET" not in preview.text
        )
        for expected in (1, 0):
            response = await client.post(
                "/api/environment/import", headers=auth, json={"names": ["OPENAI_API_KEY"]}
            )
            assert len(response.json()["imported"]) == expected
        row = app.state.store.routing_snapshot(datetime.now(UTC))[2][-1]
        assert row["secret_value"] is None and row["env_name"] == "OPENAI_API_KEY"
        assert row["plan_type"] == "UNKNOWN"
        assert (
            await client.post(
                "/api/environment/import", headers=auth, json={"names": ["UNRELATED_SECRET"]}
            )
        ).status_code == 400


@pytest.mark.parametrize(
    "path",
    [
        "/api/explain",
        "/api/integrations",
        "/api/doctor",
        "/api/environment",
        "/api/catalog",
        "/events",
    ],
)
async def test_new_management_surfaces_require_authorization(tmp_path, path):
    _, client, auth = await setup(tmp_path, lambda r: httpx.Response(200, json={}))
    async with client:
        assert (await client.get(path)).status_code == 401
        assert (
            await client.post(
                "/api/doctor", headers=auth | {"Origin": "https://evil.invalid"}, json={}
            )
        ).status_code == 403


def test_feed_limits_overflow_and_metadata_only():
    feed = ActivityFeed()
    queues = [feed.subscribe() for _ in range(64)]
    assert feed.subscribe() is None
    for _ in range(65):
        feed.publish(
            {"request_id": "id", "profile_slug": "default", "outcome": "OK", "prompt": "private"}
        )
    event = queues[0].get_nowait()
    assert event["refresh"] and "private" not in json.dumps(event)


def test_v3_migration_preserves_rollups_and_rolls_back_on_failure(tmp_path):
    store = Store(tmp_path)
    with store.connection() as conn:
        conn.execute("DROP TABLE credential_checks")
        conn.execute("PRAGMA user_version=3")
        conn.execute("INSERT INTO daily_usage VALUES('2026-10-07',99,12.34,1)")
    store = Store(tmp_path)
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("SELECT cost_usd FROM daily_usage").fetchone()[0] == 12.34
        conn.execute("PRAGMA user_version=3")  # Existing conflicting table makes migration fail.
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT cost_usd FROM daily_usage").fetchone()[0] == 12.34


def test_snippets_are_parseable_and_use_profile_alias_and_secret_references():
    for shell in ("bash", "powershell"):
        snippets = integration_snippets("http://127.0.0.1:9898/v1", "my-app", shell)["snippets"]
        compile(snippets["python"], "snippet.py", "exec")
        config = json.loads(snippets["opencode"])
        provider = config["provider"]["quotamesh"]
        assert provider["npm"] == "@ai-sdk/openai-compatible"
        assert provider["options"]["apiKey"] == "{env:QUOTAMESH_KEY}"
        assert provider["options"]["baseURL"] == "http://127.0.0.1:9898/v1"
        assert "qm/my-app" in provider["models"]


def test_cli_commands_and_missing_server_have_honest_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAMESH_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in ("doctor", "profile", "env", "import-env", "add", "reset", "tail"):
        assert name in result.stdout
    result = runner.invoke(cli, ["profile", "test", "missing"])
    assert result.exit_code == 1 and "Start QuotaMesh first" in result.stdout
