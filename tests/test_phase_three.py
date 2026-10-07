"""Wallet/profile integration and durable upgrade acceptance; only fake credentials."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_phase_two import Events, call, setup

from quotamesh.app import create_app
from quotamesh.store import Store
from quotamesh.usage import summarize

TARGET = {"provider_id": "custom", "model": "model", "input_price": 1, "output_price": 1}


def ok(_request):
    return httpx.Response(
        200,
        json={
            "choices": [],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "cost_usd": 0.6},
        },
    )


async def profile(client, auth, slug, **changes):
    return await client.post(
        "/api/profiles",
        headers=auth,
        json={"slug": slug, "name": slug.title(), "targets": [TARGET], **changes},
    )


def legacy_store(tmp_path):
    db = tmp_path / "quotamesh.db"
    with sqlite3.connect(db) as conn:
        conn.executescript((Path(__file__).parent / "fixtures/phase-two.sql").read_text())
    return db


def test_v2_upgrade_backfills_once_without_resetting_pruned_paid_spend(tmp_path):
    legacy_store(tmp_path)
    store = Store(tmp_path)
    now = datetime(2026, 10, 2, tzinfo=UTC)
    assert store.runtime_connection()["secret_value"] == "fake-legacy-secret"
    assert store.routing_snapshot(now)[4]["monthly"] == 3.25
    assert store.routing_snapshot(now)[4]["unknown_monthly"]
    totals = summarize(store.usage_rows())
    assert totals["routed_requests"] == 1 and totals["provider_cost_usd"] == 0.25
    assert store.activity()["requests"][0]["attempts"][0]["credential_label"] == "Legacy paid"
    assert summarize(Store(tmp_path).usage_rows()) == totals
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            "retained history"
            in conn.execute("SELECT value FROM settings WHERE key='usage_coverage'").fetchone()[0]
        )


def test_failed_backfill_rolls_back_whole_migration(tmp_path, monkeypatch):
    path = legacy_store(tmp_path)

    def fail(*_):
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr("quotamesh.usage.record_rollup", fail)
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "deleted_at" not in {r[1] for r in conn.execute("PRAGMA table_info(credentials)")}
        assert conn.execute("SELECT cost_usd FROM daily_usage").fetchone()[0] == 3.25


async def test_named_routing_scopes_paid_caps_and_preserves_aliases_across_restart(tmp_path):
    app, client, auth = await setup(tmp_path, ok, keys=[{"plan_type": "PAID"}], targets=[TARGET])
    async with client:
        assert (await profile(client, auth, "free-only", allow_trial=False)).status_code == 201
        assert (
            await profile(client, auth, "paid-backup", allow_paid=True, paid_daily_cap_usd=1)
        ).status_code == 201
        assert (await call(client, auth, model="qm/free-only")).status_code == 403
        first = await call(client, auth, model="qm/paid-backup", extension={"preserve": True})
        assert first.status_code == 200 and first.headers["x-quotamesh-profile"] == "paid-backup"
        assert (await call(client, auth, model="qm/paid-backup")).status_code == 200
        assert (await call(client, auth, model="qm/paid-backup")).status_code == 503
        assert (
            await profile(client, auth, "other-paid", allow_paid=True, paid_daily_cap_usd=1)
        ).status_code == 201
        assert (await call(client, auth, model="qm/other-paid")).status_code == 200
        usage = (await client.get("/api/usage?profile=paid-backup", headers=auth)).json()
        assert usage["totals"]["provider_cost_usd"] == 1.2
        assert (
            usage["totals"]["estimated_cost_usd"] == 0
        )  # Do not count price estimate a second time.
        assert app.state.store.routing_snapshot(datetime.now(UTC))[4]["daily"] == 0
    restarted = create_app(tmp_path, httpx.MockTransport(ok), allow_test_host=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://testserver"
    ) as client:
        assert (await call(client, auth, model="qm/paid-backup")).status_code == 503
        ids = {m["id"] for m in (await client.get("/v1/models", headers=auth)).json()["data"]}
        assert {"qm/default", "qm/paid-backup", "qm/free-only"} <= ids


async def test_named_stream_updates_usage_once_and_passes_extensions(tmp_path):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(
            200,
            stream=Events(
                b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":5}}\n\n',
                b"data: [DONE]\n\n",
            ),
            headers={"content-type": "text/event-stream"},
        )

    _, client, auth = await setup(tmp_path, handler)
    async with client:
        await profile(client, auth, "stream-app")
        response = await call(
            client, auth, model="qm/stream-app", stream=True, future_option={"x": 1}
        )
        assert response.status_code == 200 and "[DONE]" in response.text
        assert seen[0]["model"] == "model" and seen[0]["future_option"] == {"x": 1}
        totals = (await client.get("/api/usage?profile=stream-app", headers=auth)).json()["totals"]
        assert totals["routed_requests"] == totals["attempts"] == 1
        assert totals["input_tokens"] == 4 and totals["output_tokens"] == 5


async def test_profiles_share_real_quota_boundary_and_disabled_route_is_not_called(tmp_path):
    calls = []

    def limited(req):
        calls.append(req)
        return httpx.Response(
            429, json={"error": {"message": "rate limit"}}, headers={"Retry-After": "30"}
        )

    _, client, auth = await setup(
        tmp_path, limited, keys=[{"quota_group": "shared"}, {"quota_group": "shared"}]
    )
    async with client:
        await profile(client, auth, "second")
        await call(client, auth)
        assert (await call(client, auth, model="qm/second")).status_code == 429
        assert len(calls) == 1
        disabled = {"slug": "second", "name": "Second", "targets": [TARGET], "enabled": False}
        assert (
            await client.put("/api/profiles/second", headers=auth, json=disabled)
        ).status_code == 200
        assert (await call(client, auth, model="qm/second")).status_code == 503
        assert not (await client.get("/api/dry-run?profile=second", headers=auth)).json()[
            "selected"
        ]
        assert (await call(client, auth, model="qm/missing")).status_code == 404
        assert (await call(client, auth, model="qm/Bad Alias")).status_code == 400
        assert len(calls) == 1


async def test_archive_prevents_identity_reuse_cap_reset_and_pinned_deletion(tmp_path):
    app, client, auth = await setup(tmp_path, ok, keys=[{}])
    async with client:
        original = (
            await profile(client, auth, "pinned", targets=[TARGET | {"credential_id": 1}])
        ).json()["profile_id"]
        assert (await client.delete("/api/credentials/1", headers=auth)).status_code == 409
        assert (
            await client.patch("/api/credentials/1", headers=auth, json={"provider_id": "openai"})
        ).status_code == 409
        assert (await client.delete("/api/profiles/default", headers=auth)).status_code == 409
        assert (
            await client.put(
                "/api/profiles/pinned",
                headers=auth,
                json={"slug": "renamed", "name": "x", "targets": [TARGET]},
            )
        ).status_code == 400
        assert (await client.delete("/api/profiles/pinned", headers=auth)).status_code == 200
        assert (await profile(client, auth, "pinned")).status_code == 409
        assert (await client.delete("/api/credentials/1", headers=auth)).status_code == 200
        assert (
            await client.post("/api/credentials/1/action", headers=auth, json={"action": "enable"})
        ).status_code == 404
        added = await client.post(
            "/api/credentials",
            headers=auth,
            json={
                "provider_id": "custom",
                "label": "New",
                "plan_type": "FREE",
                "base_url": "http://127.0.0.1:8799/v1",
                "secret_value": "fake-new",
            },
        )
        assert added.json()["credential_id"] > 1
        assert (await profile(client, auth, "new-profile")).json()["profile_id"] > original
        with app.state.store.connection() as conn:
            assert not conn.execute("SELECT 1 FROM secrets WHERE credential_id=1").fetchone()
        assert (await call(client, auth, model="qm/pinned")).status_code == 404


async def test_rotation_preserves_secret_on_metadata_edits_and_keeps_quota_and_usage(
    tmp_path, monkeypatch
):
    seen = []

    def handler(req):
        seen.append(req.headers["authorization"])
        return ok(req)

    app, client, auth = await setup(tmp_path, handler, keys=[{}])
    async with client:
        await call(client, auth)
        assert (
            await client.patch(
                "/api/credentials/1",
                headers=auth,
                json={"label": "Renamed", "account_label": "Project"},
            )
        ).status_code == 200
        await call(client, auth)
        assert seen == ["Bearer fake-1", "Bearer fake-1"]
        with app.state.store.connection() as conn:
            conn.execute("UPDATE credentials SET status='INVALID' WHERE id=1")
            conn.execute(
                "INSERT INTO quota_state VALUES('custom:account-1','other-model','COOLDOWN','2035-01-01T00:00:00+00:00',1,'rate_limit')"
            )
        assert (
            await client.patch(
                "/api/credentials/1", headers=auth, json={"secret_value": "fake-rotated"}
            )
        ).status_code == 200
        assert (
            app.state.store.safe_routing(datetime.now(UTC))["credentials"][0]["status"] == "ACTIVE"
        )
        await call(client, auth)
        assert seen[-1] == "Bearer fake-rotated"
        monkeypatch.setenv("QM_FAKE_ROTATION", "fake-env-secret")
        assert (
            await client.patch(
                "/api/credentials/1", headers=auth, json={"env_name": "QM_FAKE_ROTATION"}
            )
        ).status_code == 200
        await call(client, auth)
        assert seen[-1] == "Bearer fake-env-secret"
        assert (
            next(
                s
                for s in app.state.store.safe_routing(datetime.now(UTC))["quota_states"]
                if s["model"] == "other-model"
            )["status"]
            == "COOLDOWN"
        )
        assert summarize(app.state.store.usage_rows())["routed_requests"] == 4
        assert (
            await client.patch(
                "/api/credentials/1",
                headers=auth,
                json={"secret_value": "sensitive-fake", "env_name": "also-set"},
            )
        ).status_code == 409


async def test_shared_wallet_credit_never_doubles_and_unknown_cost_remains_unknown(tmp_path):
    _, client, auth = await setup(
        tmp_path,
        ok,
        keys=[
            {"plan_type": "TRIAL_CREDIT", "quota_group": "shared"},
            {"plan_type": "TRIAL_CREDIT", "quota_group": "shared"},
        ],
    )
    async with client:
        for identifier in (1, 2):
            assert (
                await client.patch(
                    f"/api/credentials/{identifier}",
                    headers=auth,
                    json={"starting_credit_usd": 10, "trial_expires_at": "2035-01-01"},
                )
            ).status_code == 200
        await call(client, auth)
        data = (await client.get("/api/wallet", headers=auth)).json()
        card = data["buckets"]["TRIAL_CREDIT"][0]
        assert len(data["buckets"]["TRIAL_CREDIT"]) == 1 and len(card["keys"]) == 2
        assert card["starting_credit_usd"] == 10 and card["estimated_remaining_usd"] == 9.4
        assert (
            card["remaining_quota"] is None and card["sources"]["starting_credit_usd"] == "MANUAL"
        )
        assert card["allocated_profiles"][0]["slug"] == "default"
        await client.patch("/api/credentials/2", headers=auth, json={"starting_credit_usd": 20})
        card = (await client.get("/api/wallet", headers=auth)).json()["buckets"]["TRIAL_CREDIT"][0]
        assert card["conflicting_credit"] and card["estimated_remaining_usd"] is None
    _, client, auth = await setup(
        tmp_path / "unknown",
        lambda _: httpx.Response(200, json={"choices": [], "usage": {"cost": 100}}),
        keys=[{"plan_type": "TRIAL_CREDIT"}],
    )
    async with client:
        await client.patch("/api/credentials/1", headers=auth, json={"starting_credit_usd": 10})
        await call(client, auth)
        card = (await client.get("/api/wallet", headers=auth)).json()["buckets"]["TRIAL_CREDIT"][0]
        assert card["estimated_remaining_usd"] is None and card["usage"]["unknown_cost"] == 1
        assert card["usage"]["input_tokens"] is None and card["usage"]["input_missing"] == 1


async def test_fallback_activity_distinguishes_requests_and_attempts_and_is_private(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return (
            httpx.Response(401, json={"error": {"message": "secret-body-do-not-store"}})
            if len(seen) == 1
            else ok(req)
        )

    app, client, auth = await setup(tmp_path, handler)
    async with client:
        await profile(client, auth, "work")
        response = await call(
            client, auth, model="qm/work", messages=[{"role": "user", "content": "private-prompt"}]
        )
        assert response.status_code == 200
        activity = (await client.get("/api/activity?profile=work", headers=auth)).json()
        req = activity["requests"][0]
        assert req["attempt_count"] == 2 and [a["attempt_idx"] for a in req["attempts"]] == [1, 2]
        totals = summarize(app.state.store.usage_rows())
        assert (
            totals["routed_requests"] == 1 and totals["attempts"] == 2 and totals["failures"] == 1
        )
        for path in (
            "/api/wallet",
            "/api/profiles",
            "/api/usage",
            "/api/activity",
            "/api/status?profile=work",
        ):
            output = (await client.get(path, headers=auth)).text
            for forbidden in (
                "fake-1",
                "fake-2",
                "private-prompt",
                "secret-body-do-not-store",
                "secret_value",
            ):
                assert forbidden not in output
        with app.state.store.connection() as conn:
            stored = json.dumps([dict(r) for r in conn.execute("SELECT * FROM attempts")])
        assert "private-prompt" not in stored and "secret-body-do-not-store" not in stored


def test_activity_cursor_handles_interleaved_traces_and_retention_preserves_rollups(tmp_path):
    store = Store(tmp_path)
    now = datetime.now(UTC)
    for request_id, idx in [("first", 1), ("second", 1), ("first", 2), ("third", 1)]:
        store.record_attempt(
            ts=now.isoformat(),
            request_id=request_id,
            profile_id=1,
            profile_slug="default",
            attempt_idx=idx,
            provider_id="custom",
            model="model",
            credential_id=1,
            plan_type="FREE",
            credential_label="Fake",
            outcome="OK",
            streamed=0,
            provider_cost_usd=0.1,
        )
    first = store.activity(limit=1)
    assert first["requests"][0]["request_id"] == "third"
    second = store.activity(limit=1, before=first["next_before"])
    assert (
        second["requests"][0]["request_id"] == "first"
        and second["requests"][0]["attempt_count"] == 2
    )
    third = store.activity(limit=1, before=second["next_before"])
    assert third["requests"][0]["request_id"] == "second"
    totals = summarize(store.usage_rows())
    store.prune_history(now, max_rows=2)
    assert store.activity()["requests"][0]["request_id"] == "third"
    assert summarize(store.usage_rows()) == totals
    store.prune_history(now + timedelta(days=31))
    assert not store.activity()["requests"] and summarize(Store(tmp_path).usage_rows()) == totals


@pytest.mark.parametrize("path", ["/api/wallet", "/api/profiles", "/api/activity", "/api/usage"])
async def test_management_requires_auth(tmp_path, path):
    app = create_app(tmp_path, allow_test_host=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.delete("/api/credentials/1")).status_code == 401
        assert (await client.delete("/api/profiles/work")).status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"label": None},
        {"priority": 10001},
        {"starting_credit_usd": -1},
        {"base_url": "http://example.com/v1"},
        {"secret_value": ""},
        {"unexpected": "sensitive-value"},
        {"trial_expires_at": "invalid"},
    ],
)
async def test_credential_patch_validation_never_echoes_inputs(tmp_path, payload):
    _, client, auth = await setup(tmp_path, ok)
    async with client:
        response = await client.patch("/api/credentials/1", headers=auth, json=payload)
        assert response.status_code in {400, 409}
        assert "sensitive-value" not in response.text


async def test_minimal_add_needs_no_model_or_label_and_does_not_enable_paid(tmp_path):
    app = create_app(tmp_path, allow_test_host=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        auth = {"Authorization": "Bearer " + app.state.local_key}
        response = await client.post(
            "/api/credentials",
            headers=auth,
            json={"provider_id": "openai", "plan_type": "PAID", "secret_value": "fake-key"},
        )
        assert response.status_code == 201
        credential = app.state.store.safe_routing(datetime.now(UTC))["credentials"][0]
        assert credential["label"] and credential["plan_type"] == "PAID"
        assert "secret_value" not in credential
        assert (
            await client.post(
                "/api/policy",
                headers=auth,
                json={"targets": [{"provider_id": "openai", "model": "test"}]},
            )
        ).status_code == 200
        assert (await call(client, auth)).status_code == 403


async def test_cost_sources_remain_truthful_after_pruning_and_credential_delete(tmp_path):
    app, client, auth = await setup(
        tmp_path, ok, keys=[{"quota_group": "shared"}, {"quota_group": "shared"}]
    )
    async with client:
        initial = (await client.get("/api/wallet", headers=auth)).json()["buckets"]["FREE"][0]
        assert (
            initial["usage"]["provider_cost_count"] == 0
            and initial["usage"]["sources"]["provider_cost_usd"] == "UNKNOWN"
        )
        await call(client, auth)
        app.state.store.prune_history(datetime.now(UTC) + timedelta(days=31))
        assert (await client.delete("/api/credentials/1", headers=auth)).status_code == 200
        card = (await client.get("/api/wallet", headers=auth)).json()["buckets"]["FREE"][0]
        assert (
            card["usage"]["provider_cost_usd"] == 0.6 and card["usage"]["provider_cost_count"] == 1
        )
        assert card["usage"]["sources"]["provider_cost_usd"] == "PROVIDER"
        assert card["today"]["routed_requests"] == 1 and len(card["models"]) == 1
        assert len(card["keys"]) == 1 and not app.state.store.activity()["requests"]


async def test_late_old_secret_failure_cannot_invalidate_rotated_key(tmp_path):
    holder = {}
    calls = []

    def handler(request):
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            holder["app"].state.store.edit_credential(1, {"secret_value": "fake-new-key"})
            return httpx.Response(401, json={"error": {"message": "old key rejected"}})
        return ok(request)

    app, client, auth = await setup(tmp_path, handler, keys=[{}])
    holder["app"] = app
    async with client:
        assert (await call(client, auth)).status_code == 401
        assert (
            app.state.store.safe_routing(datetime.now(UTC))["credentials"][0]["status"] == "ACTIVE"
        )
        assert (await call(client, auth)).status_code == 200
        assert calls == ["Bearer fake-1", "Bearer fake-new-key"]


async def test_profile_deleted_during_attempt_does_not_route_fallback_elsewhere(tmp_path):
    holder, calls = {}, []

    def handler(req):
        calls.append(req)
        holder["app"].state.store.delete_profile("transient")
        return httpx.Response(429, json={"error": {"message": "rate limit"}})

    app, client, auth = await setup(tmp_path, handler)
    holder["app"] = app
    async with client:
        identifier = (await profile(client, auth, "transient")).json()["profile_id"]
        assert (await call(client, auth, model="qm/transient")).status_code == 429
        assert len(calls) == 1
        assert app.state.store.usage_rows()[0]["profile_id"] == identifier
        assert (await call(client, auth, model="qm/transient")).status_code == 404


async def test_implausible_cost_stays_unknown_without_breaking_wallet_or_caps(tmp_path):
    app, client, auth = await setup(
        tmp_path,
        lambda _: httpx.Response(200, json={"choices": [], "usage": {"cost_usd": 1e308}}),
        keys=[{"plan_type": "PAID"}],
        targets=[TARGET],
        policy={"allow_paid": True, "paid_daily_cap_usd": 1},
    )
    async with client:
        assert (await call(client, auth)).status_code == 200
        assert (await client.get("/api/wallet", headers=auth)).status_code == 200
        assert summarize(app.state.store.usage_rows())["unknown_cost"] == 1
        assert (await call(client, auth)).status_code == 503
