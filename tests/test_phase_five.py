"""Release safety and passive evidence: no real provider access."""

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_phase_two import call, setup
from typer.testing import CliRunner

from quotamesh.app import create_app
from quotamesh.cli import app as cli
from quotamesh.lifecycle import process_lock
from quotamesh.observations import rate_observations
from quotamesh.store import Store


async def test_initial_setup_is_authorized_atomic_and_paid_safe(tmp_path):
    app = create_app(
        tmp_path,
        httpx.MockTransport(lambda r: pytest.fail("Setup must be passive")),
        allow_test_host=True,
    )
    auth = {"Authorization": "Bearer " + app.state.local_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.post("/api/setup", json={})).status_code == 401
        key = await client.post(
            "/api/credentials",
            headers=auth,
            json={
                "provider_id": "openai",
                "plan_type": "PAID",
                "secret_value": "release-synthetic-secret",
            },
        )
        identifier = key.json()["credential_id"]
        assert (
            await client.post("/api/setup", headers=auth, json={"credential_id": 999, "model": "m"})
        ).status_code == 404
        assert app.state.store.profiles() == []
        result = await client.post(
            "/api/setup", headers=auth, json={"credential_id": identifier, "model": "m"}
        )
        assert result.status_code == 201 and result.json()["allow_paid"] is False
        assert (await call(client, auth)).status_code == 403
        assert (
            await client.post(
                "/api/setup", headers=auth, json={"credential_id": identifier, "model": "other"}
            )
        ).status_code == 409
        assert app.state.store.safe_routing(datetime.now(UTC))["targets"][0]["model"] == "m"
        bad = await client.post(
            "/api/setup", headers=auth, json={"credential_id": identifier, "model": "secret\n"}
        )
        assert bad.status_code == 400 and "secret" not in bad.text


async def test_duplicate_create_rotation_and_environment_are_safe(tmp_path, monkeypatch):
    app, client, auth = await setup(tmp_path, lambda r: httpx.Response(200, json={"choices": []}))
    async with client:
        body = {
            "provider_id": "custom",
            "plan_type": "PAID",
            "secret_value": "fake-1",
            "base_url": "http://127.0.0.1:8799/v1",
        }
        result = await client.post("/api/credentials", headers=auth, json=body)
        assert result.status_code == 409 and "fake-1" not in result.text
        result = await client.patch(
            "/api/credentials/2", headers=auth, json={"secret_value": "fake-1"}
        )
        assert result.status_code == 409
        assert app.state.store.routing_snapshot(datetime.now(UTC))[2][1]["secret_value"] == "fake-2"
        monkeypatch.setenv("RELEASE_ALIAS", "fake-1")
        body.pop("secret_value")
        body["env_name"] = "RELEASE_ALIAS"
        assert (await client.post("/api/credentials", headers=auth, json=body)).status_code == 409


async def test_legacy_aliases_cannot_retry_or_bypass_cooldown(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(429, headers={"retry-after": "120"}, json={"error": {}})

    app, client, auth = await setup(tmp_path, upstream)
    with app.state.store.connection() as conn:
        conn.execute("UPDATE secrets SET secret_value='fake-1' WHERE credential_id=2")
    async with client:
        assert (await call(client, auth)).status_code == 429
        await call(client, auth)
    assert calls == ["Bearer fake-1"]


@pytest.mark.parametrize("provider,window", [("openai", "provider window"), ("groq", "day")])
def test_rate_headers_are_bounded_dated_and_not_fabricated(provider, window):
    now = datetime.now(UTC)
    rows = rate_observations(
        provider,
        {
            "x-ratelimit-remaining-requests": "12",
            "x-ratelimit-reset-requests": "2m",
            "authorization": "secret",
        },
        now,
    )
    assert rows[0]["remaining"] == 12 and rows[0]["window"] == window
    assert datetime.fromisoformat(rows[0]["stale_at"]) == now + timedelta(seconds=60)
    assert "secret" not in str(rows)
    for value in ("-1", "NaN", "1.2", "9" * 100):
        assert rate_observations(provider, {"x-ratelimit-remaining-requests": value}, now) == []
    assert rate_observations("custom", {"x-ratelimit-remaining-requests": "12"}, now) == []


async def test_passive_rates_and_timing_are_durable_and_rotation_guarded(tmp_path):
    app, client, auth = await setup(
        tmp_path,
        lambda r: httpx.Response(
            200,
            headers={"x-ratelimit-remaining-requests": "9", "x-ratelimit-reset-requests": "5s"},
            json={"choices": []},
        ),
        keys=[{"provider_id": "groq"}],
        targets=[{"provider_id": "groq", "model": "m"}],
    )
    async with client:
        assert (await call(client, auth)).status_code == 200
    store = app.state.store
    assert store.recent_attempts()[0]["ttfb_ms"] >= 0
    assert store.usage_rows()[0]["last_ttfb_ms"] >= 0
    assert store.rates(datetime.now(UTC))[0]["source"] == "PROVIDER"
    assert store.rates(datetime.now(UTC) + timedelta(seconds=6))[0]["stale"]
    assert Store(tmp_path).rates(datetime.now(UTC))[0]["remaining"] == 9
    store.edit_credential(1, {"secret_value": "rotated", "env_name": None})
    assert store.rates(datetime.now(UTC)) == []


def test_v4_migration_preserves_spend_and_rolls_back_conflicts(tmp_path):
    store = Store(tmp_path)
    with store.connection() as conn:
        conn.execute("DROP TABLE rate_observations")
        conn.execute("ALTER TABLE attempts DROP COLUMN ttfb_ms")
        conn.execute("ALTER TABLE usage_rollups DROP COLUMN last_ttfb_ms")
        conn.execute("PRAGMA user_version=4")
        conn.execute("INSERT INTO daily_usage VALUES('2026-10-07',1,12.34,1)")
    migrated = Store(tmp_path)
    with migrated.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT cost_usd FROM daily_usage").fetchone()[0] == 12.34
        conn.execute("ALTER TABLE attempts DROP COLUMN ttfb_ms")
        conn.execute("ALTER TABLE usage_rollups DROP COLUMN last_ttfb_ms")
        conn.execute("PRAGMA user_version=4")
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "ttfb_ms" not in {r[1] for r in conn.execute("PRAGMA table_info(attempts)")}


def test_process_lock_excludes_other_owner_and_releases(tmp_path):
    with process_lock(tmp_path), pytest.raises(RuntimeError, match="Another QuotaMesh"), process_lock(tmp_path):
        pytest.fail("Second owner")
    with process_lock(tmp_path):
        pass


async def test_lifespan_closes_client_when_lock_is_unavailable(tmp_path):
    app = create_app(tmp_path)
    with process_lock(tmp_path), pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pass
    assert app.state.client.is_closed


def test_version_does_not_open_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAMESH_DATA_DIR", str(tmp_path / "absent"))
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0 and result.stdout.strip() == "0.1.0"
    assert not (tmp_path / "absent").exists()
