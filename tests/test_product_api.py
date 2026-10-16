"""Request traces and the wallet's daily series: metadata only, honest cost sources."""

from datetime import UTC, datetime, timedelta

import httpx
from test_phase_two import call, setup

from quotamesh.store import Store


def fallback_handler(request):
    if request.headers["authorization"] == "Bearer fake-1":
        return httpx.Response(
            429, json={"error": {"message": "rate limit"}}, headers={"Retry-After": "60"}
        )
    return httpx.Response(
        200,
        json={"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2}},
    )


async def test_request_trace_shape_orders_attempts_and_explains_skips(tmp_path):
    _, client, auth = await setup(tmp_path, fallback_handler)
    async with client:
        response = await call(
            client, auth, messages=[{"role": "user", "content": "private-prompt"}]
        )
        assert response.status_code == 200
        request_id = response.headers["x-quotamesh-request-id"]
        trace = await client.get(f"/api/requests/{request_id}", headers=auth)
    assert trace.status_code == 200
    for forbidden in ("private-prompt", "fake-1", "fake-2", "secret_value", "skipped_json"):
        assert forbidden not in trace.text
    data = trace.json()
    assert data["request_id"] == request_id and data["profile_slug"] == "default"
    assert data["started_at"] == data["attempts"][0]["ts"]
    first, second = data["attempts"]
    assert (first["attempt_idx"], first["outcome"], first["error_class"]) == (
        1,
        "UPSTREAM_ERROR",
        "rate_limit",
    )
    assert (second["attempt_idx"], second["outcome"], second["http_status"]) == (2, "OK", 200)
    assert second["credential_label"] == "Key 2" and second["plan_type"] == "FREE"
    assert second["input_tokens"] == 8 and second["output_tokens"] == 2
    assert second["streamed"] == 0 and second["latency_ms"] >= 0
    assert second["skipped"] == [
        {
            "label": "Key 1",
            "skip_reason": "cooldown",
            "explanation": "This quota group and model are temporarily rate limited.",
        }
    ]
    assert data["totals"] == {
        "attempts": 2,
        "input_tokens": 8,
        "output_tokens": 2,
        "known_cost_usd": 0,
        "cost_source": "LOCAL",
    }


async def test_request_trace_is_authorized_validated_and_gone_after_pruning(tmp_path):
    app, client, auth = await setup(tmp_path, fallback_handler)
    async with client:
        request_id = (await call(client, auth)).headers["x-quotamesh-request-id"]
        assert (await client.get(f"/api/requests/{request_id}")).status_code == 401
        for bad in ("0" * 32, request_id.upper(), request_id[:-1], "../" + request_id):
            response = await client.get(f"/api/requests/{bad}", headers=auth)
            assert response.status_code == 404
        app.state.store.prune_history(datetime.now(UTC) + timedelta(days=31))
        response = await client.get(f"/api/requests/{request_id}", headers=auth)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "request_not_found"


def record(store, ts, request_id, **values):
    store.record_attempt(
        **{
            "ts": ts.isoformat(),
            "request_id": request_id,
            "profile_id": 1,
            "profile_slug": "default",
            "attempt_idx": 1,
            "provider_id": "gemini",
            "model": "m",
            "credential_id": 1,
            "plan_type": "FREE",
            "credential_label": "Key",
            "outcome": "OK",
            "streamed": 1,
            **values,
        }
    )


def test_request_trace_cost_sources_distinguish_provider_and_unknown(tmp_path):
    from quotamesh.routes.wallet import trace_view

    store = Store(tmp_path)
    now = datetime.now(UTC)
    record(store, now, "a" * 32, provider_cost_usd=0.25, input_tokens=3, output_tokens=4)
    assert trace_view(store.request_trace("a" * 32))["totals"]["cost_source"] == "PROVIDER"
    record(store, now, "b" * 32, outcome="UPSTREAM_ERROR", error_class="network")
    totals = trace_view(store.request_trace("b" * 32))["totals"]
    assert totals["cost_source"] == "UNKNOWN" and totals["known_cost_usd"] == 0
    assert totals["input_tokens"] is None
    store.close()


async def test_wallet_daily_is_fourteen_zero_filled_days_oldest_first(tmp_path):
    app, client, auth = await setup(tmp_path, fallback_handler)
    store = app.state.store
    now = datetime.now(UTC)
    record(store, now - timedelta(days=3), "c" * 32, estimated_cost_usd=0.0001, input_tokens=5)
    record(
        store,
        now - timedelta(days=3),
        "d" * 32,
        provider_id="openai",
        plan_type="PAID",
        provider_cost_usd=0.0002,
        attempt_idx=2,
        outcome="UPSTREAM_ERROR",
    )
    record(store, now - timedelta(days=20), "e" * 32)
    async with client:
        await call(client, auth)
        daily = (await client.get("/api/wallet", headers=auth)).json()["daily"]
    assert len(daily) == 14
    assert [entry["day"] for entry in daily] == [
        (now.date() - timedelta(days=offset)).isoformat() for offset in range(13, -1, -1)
    ]
    today, earlier = daily[-1], daily[-4]
    assert today["routed_requests"] == 1 and today["attempts"] == 2 and today["failures"] == 1
    assert set(today["by_provider"]) == {"custom"}
    assert earlier["routed_requests"] == 1 and earlier["attempts"] == 2
    assert earlier["failures"] == 1 and earlier["input_tokens"] == 5
    assert earlier["known_cost_usd"] == 0.0003
    assert earlier["by_provider"] == {
        "gemini": {"routed_requests": 1, "known_cost_usd": 0.0001},
        "openai": {"routed_requests": 0, "known_cost_usd": 0.0002},
    }
    quiet = daily[0]
    assert quiet["attempts"] == 0 and quiet["by_provider"] == {} and quiet["known_cost_usd"] == 0
    assert sum(entry["attempts"] for entry in daily) == 4  # The 20-day-old row is outside.
