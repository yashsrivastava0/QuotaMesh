"""Live integration matrix from the product spec, without real provider quota."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest

from quotamesh.app import create_app


async def setup(tmp_path, handler, *, keys=None, targets=None, policy=None):
    app = create_app(tmp_path, httpx.MockTransport(handler), allow_test_host=True)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    auth = {"Authorization": "Bearer " + app.state.local_key}
    for idx, overrides in enumerate(keys or [{}, {}], 1):
        response = await client.post(
            "/api/credentials",
            headers=auth,
            json={
                "provider_id": "custom",
                "label": f"Key {idx}",
                "plan_type": "FREE",
                "model": "unused",
                "secret_value": f"fake-{idx}",
                "base_url": "http://127.0.0.1:8799/v1",
                "quota_group": f"account-{idx}",
                **overrides,
            },
        )
        assert response.status_code == 201, response.text
    response = await client.post(
        "/api/policy",
        headers=auth,
        json={
            "targets": targets or [{"provider_id": "custom", "model": "model"}],
            **(policy or {}),
        },
    )
    assert response.status_code == 200, response.text
    await client.aclose()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    return app, client, auth


async def call(client, auth, **extra):
    return await client.post(
        "/v1/chat/completions", headers=auth, json={"model": "qm/default", "messages": [], **extra}
    )


@pytest.mark.parametrize("status", [401, 402, 429])
async def test_retryable_key_failure_falls_back(tmp_path, status):
    calls = []

    def handler(req):
        calls.append(req.headers["authorization"])
        return httpx.Response(
            status if len(calls) == 1 else 200,
            json={"error": {"message": "rate limit"}} if len(calls) == 1 else {"choices": []},
            headers={"Retry-After": "120"},
        )

    app, client, auth = await setup(tmp_path, handler)
    async with client:
        response = await call(client, auth, future_extension={"a": 1})
        assert response.status_code == 200
        assert response.headers["x-quotamesh-attempts"] == "2"
        assert response.headers["x-quotamesh-fallback"] == "true"
        assert calls == ["Bearer fake-1", "Bearer fake-2"]
        assert len(app.state.store.recent_attempts()) == 2


async def test_shared_group_skips_sibling_survives_restart_and_other_model_works(tmp_path):
    calls = []

    def handler(req):
        calls.append(req.headers["authorization"])
        return httpx.Response(
            429, json={"error": {"message": "rate limit"}}, headers={"Retry-After": "5"}
        )

    _app, client, auth = await setup(
        tmp_path, handler, keys=[{"quota_group": "same"}, {"quota_group": "same"}]
    )
    async with client:
        assert (await call(client, auth)).status_code == 429
        assert calls == ["Bearer fake-1"]
    restarted = create_app(tmp_path, httpx.MockTransport(handler), allow_test_host=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://testserver"
    ) as client:
        response = await call(client, auth)
        assert response.status_code == 429 and response.headers["retry-after"] == "5"
        assert calls == ["Bearer fake-1"]
        dry = (await client.get("/api/dry-run", headers=auth)).json()
        assert all(d["skip_reason"] == "cooldown" for d in dry["decisions"])
        await client.post("/api/credentials/1/action", headers=auth, json={"action": "reset"})
        assert (await client.get("/api/dry-run", headers=auth)).json()["selected"][
            "credential_id"
        ] == 1


@pytest.mark.parametrize("status", [400, 422])
async def test_bad_request_is_terminal(tmp_path, status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, json={"error": {"message": "malformed input"}})

    _, client, auth = await setup(tmp_path, handler)
    async with client:
        assert (await call(client, auth)).status_code == status
    assert len(calls) == 1


async def test_context_length_skips_pool_and_moves_to_next_model(tmp_path):
    calls = []

    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append((req.headers["authorization"], model))
        return (
            httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})
            if model == "short"
            else httpx.Response(200, json={"choices": []})
        )

    _, client, auth = await setup(
        tmp_path,
        handler,
        targets=[
            {"provider_id": "custom", "model": "short"},
            {"provider_id": "custom", "model": "long"},
        ],
    )
    async with client:
        assert (await call(client, auth)).status_code == 200
    assert calls == [("Bearer fake-1", "short"), ("Bearer fake-1", "long")]


class Events(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            if isinstance(chunk, float):
                await asyncio.sleep(chunk)
            else:
                yield chunk

    async def aclose(self):
        self.closed = True


FIRST = b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
DONE = b"data: [DONE]\n\n"


async def test_first_sse_error_falls_back_and_comment_does_not_commit(tmp_path):
    streams = [
        Events(b": keepalive\n\n", b'data: {"error":{"code":429}}\n\n'),
        Events(b": hi\n\n" + FIRST + DONE),
    ]
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200, stream=streams[len(calls) - 1], headers={"content-type": "text/event-stream"}
        )

    app, client, auth = await setup(tmp_path, handler)
    async with client:
        response = await call(client, auth, stream=True)
        assert response.status_code == 200
        assert response.headers["x-quotamesh-attempts"] == "2"
        assert response.content == b": hi\n\n" + FIRST + DONE
    assert all(stream.closed for stream in streams)
    assert app.state.store.recent_attempts()[0]["outcome"] == "OK"


@pytest.mark.parametrize(
    "tail", [httpx.ReadError("cut"), b"", b'data: {"error":{"message":"cut"}}\n\n']
)
async def test_no_midstream_fallback_and_truncation_detected(tmp_path, tail):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200, stream=Events(FIRST, tail), headers={"content-type": "text/event-stream"}
        )

    app, client, auth = await setup(tmp_path, handler)
    async with client:
        response = await call(client, auth, stream=True)
        assert response.status_code == 200 and response.content.startswith(FIRST)
    assert len(calls) == 1
    assert app.state.store.recent_attempts()[0]["outcome"] == "MID_STREAM_FAILURE"


async def test_stream_usage_cost_paid_cap_and_persistence(tmp_path):
    usage = b'data: {"choices":[],"usage":{"prompt_tokens":1000000,"completion_tokens":0}}\n\n'
    app, client, auth = await setup(
        tmp_path,
        lambda req: httpx.Response(
            200, stream=Events(FIRST, usage, DONE), headers={"content-type": "text/event-stream"}
        ),
        keys=[{"plan_type": "PAID"}],
        targets=[{"provider_id": "custom", "model": "model", "input_price": 2, "output_price": 3}],
        policy={"allow_paid": True, "paid_daily_cap_usd": 1},
    )
    async with client:
        assert (await call(client, auth, stream=True)).status_code == 200
        assert (await call(client, auth)).status_code == 503
        status = (await client.get("/api/status", headers=auth)).json()
        assert status["routing"]["usage"]["daily"] == 2
        assert status["decisions"][0]["skip_reason"] == "cap_reached"
    with app.state.store.connection() as conn:
        conn.execute("DELETE FROM attempts")
    assert app.state.store.routing_snapshot(datetime.now(UTC))[4]["daily"] == 2


async def test_paid_default_unknown_price_expiry_and_attempt_limit(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    app, client, auth = await setup(
        tmp_path,
        handler,
        keys=[{"trial_expires_at": "2020-01-01"}, {"plan_type": "PAID"}, {}, {}],
        policy={"max_attempts": 1},
    )
    async with client:
        dry = (await client.get("/api/dry-run", headers=auth)).json()
        assert dry["selected"]["credential_id"] == 3
        response = await call(client, auth)
        assert response.status_code == 401 and len(calls) == 1
        assert calls[0].headers["authorization"] == "Bearer fake-3"
        assert app.state.store.recent_attempts()[0]["attempt_idx"] == 1
        assert (
            await client.post(
                "/api/policy",
                headers=auth,
                json={
                    "allow_paid": True,
                    "paid_daily_cap_usd": 1,
                    "targets": [{"provider_id": "custom", "model": "paid", "credential_id": 2}],
                },
            )
        ).status_code == 200
        dry = (await client.get("/api/dry-run", headers=auth)).json()
        assert dry["selected"] is None and dry["decisions"][0]["skip_reason"] == "unknown_price"
        assert (await call(client, auth)).status_code == 503
        assert len(calls) == 1


async def test_total_deadline_cancels_upstream_and_closes_stream(tmp_path):
    stream = Events(0.2, FIRST, DONE)
    app, client, auth = await setup(
        tmp_path,
        lambda req: httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        ),
        policy={"first_event_timeout_s": 0.05},
    )
    async with client:
        response = await call(client, auth, stream=True)
        assert response.status_code == 504
    assert stream.closed
    assert len(app.state.store.recent_attempts()) == 1


async def test_parallel_calls_recompute_cooldown_before_selection(tmp_path):
    calls = []

    async def handler(req):
        calls.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer fake-1":
            return httpx.Response(429, json={"error": {}}, headers={"Retry-After": "120"})
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"choices": []})

    _, client, auth = await setup(tmp_path, handler)
    async with client:
        assert (await call(client, auth)).status_code == 200
        responses = await asyncio.gather(*(call(client, auth) for _ in range(20)))
        assert all(r.status_code == 200 for r in responses)
    assert calls.count("Bearer fake-1") == 1
    assert calls.count("Bearer fake-2") == 21


async def test_diagnostic_write_failure_keeps_response_and_fails_closed_for_paid(
    tmp_path, monkeypatch
):
    app, client, auth = await setup(
        tmp_path,
        lambda req: httpx.Response(200, json={"choices": []}),
        keys=[{"plan_type": "PAID"}],
        policy={"allow_paid": True},
    )

    def fail(**values):
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(app.state.store, "record_attempt", fail)
    async with client:
        assert (await call(client, auth)).status_code == 200
        assert app.state.diagnostic_write_failures == 1
        assert (await call(client, auth)).status_code == 503


async def test_management_auth_validation_and_secret_redaction(tmp_path):
    _, client, auth = await setup(tmp_path, lambda req: httpx.Response(200, json={"choices": []}))
    async with client:
        assert (await client.get("/api/dry-run")).status_code == 401
        assert (
            await client.post("/api/policy", headers=auth, json={"targets": []})
        ).status_code == 400
        assert (
            await client.post(
                "/api/policy",
                headers=auth,
                json={"targets": [{"provider_id": "openai", "model": "bad", "credential_id": 1}]},
            )
        ).status_code == 400
        assert (await client.post("/api/credentials", headers=auth, json=[])).status_code == 400
        assert (
            await client.post(
                "/api/credentials",
                headers=auth,
                json={
                    "provider_id": "custom",
                    "plan_type": "FREE",
                    "label": "bad",
                    "model": "bad",
                    "base_url": "http://evil.example",
                    "secret_value": "do-not-echo",
                },
            )
        ).status_code == 400
        status = (await client.get("/api/status", headers=auth)).text
        assert "fake-1" not in status and "secret_value" not in status


async def test_provider_model_errors_move_to_next_target_without_invalidating_key(tmp_path):
    calls = []

    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append(model)
        return httpx.Response(
            503 if model == "broken" else 200,
            json={"error": {"message": "overloaded"}} if model == "broken" else {"choices": []},
        )

    app, client, auth = await setup(
        tmp_path,
        handler,
        targets=[
            {"provider_id": "custom", "model": "broken"},
            {"provider_id": "custom", "model": "working"},
        ],
    )
    async with client:
        response = await call(client, auth)
        assert response.status_code == 200 and calls == ["broken", "working"]
        assert all(
            c["status"] == "ACTIVE"
            for c in app.state.store.safe_routing(datetime.now(UTC))["credentials"]
        )
        dry = (await client.get("/api/dry-run", headers=auth)).json()
        assert dry["decisions"][0]["skip_reason"] == "degraded"


async def test_transport_error_and_malformed_json_fallback(tmp_path):
    calls = []

    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append(model)
        if model == "network":
            raise httpx.ConnectError("synthetic failure")
        if model == "bad-json":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, json={"choices": []})

    _, client, auth = await setup(
        tmp_path,
        handler,
        targets=[
            {"provider_id": "custom", "model": model}
            for model in ("network", "bad-json", "working")
        ],
    )
    async with client:
        response = await call(client, auth)
        assert response.status_code == 200 and response.headers["x-quotamesh-attempts"] == "3"
    assert calls == ["network", "bad-json", "working"]


async def test_paid_unknown_cost_blocks_next_request_with_cap_and_override_works(tmp_path):
    app, client, auth = await setup(
        tmp_path,
        lambda req: httpx.Response(200, json={"choices": []}),
        keys=[{"plan_type": "PAID"}],
        policy={"allow_paid": True, "paid_daily_cap_usd": 1},
        targets=[{"provider_id": "custom", "model": "model", "input_price": 1, "output_price": 1}],
    )
    async with client:
        assert (await call(client, auth)).status_code == 200
        assert (await call(client, auth)).status_code == 503
        assert app.state.store.safe_routing(datetime.now(UTC))["usage"]["unknown"]
        await client.post(
            "/api/policy",
            headers=auth,
            json={
                "allow_paid": True,
                "allow_unknown_price": True,
                "paid_daily_cap_usd": 1,
                "targets": [{"provider_id": "custom", "model": "model"}],
            },
        )
        assert (await call(client, auth)).status_code == 200


async def test_dry_live_parity_when_accounting_failed_survives_restart(tmp_path, monkeypatch):
    app, client, auth = await setup(
        tmp_path,
        lambda req: httpx.Response(200, json={"choices": []}),
        keys=[{"plan_type": "PAID"}],
        policy={"allow_paid": True},
    )

    def fail(**values):
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(app.state.store, "record_attempt", fail)
    async with client:
        assert (await call(client, auth)).status_code == 200
        assert (await client.get("/api/dry-run", headers=auth)).json()["selected"] is None
    restarted = create_app(
        tmp_path,
        httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": []})),
        allow_test_host=True,
    )
    assert restarted.state.accounting_failed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://testserver"
    ) as client:
        assert (await call(client, auth)).status_code == 503
        assert (await client.get("/api/dry-run", headers=auth)).json()["decisions"][0][
            "skip_reason"
        ] == "accounting_unavailable"


async def test_late_success_does_not_erase_newer_shared_cooldown(tmp_path):
    success_started = asyncio.Event()
    throttle_completed = asyncio.Event()
    requests = 0

    async def handler(req):
        nonlocal requests
        requests += 1
        if requests == 1:
            success_started.set()
            await throttle_completed.wait()
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(429, json={"error": {}}, headers={"Retry-After": "120"})

    _, client, auth = await setup(tmp_path, handler, keys=[{}])
    async with client:
        success = asyncio.create_task(call(client, auth))
        await success_started.wait()
        assert (await call(client, auth)).status_code == 429
        throttle_completed.set()
        assert (await success).status_code == 200
        assert (await client.get("/api/dry-run", headers=auth)).json()["selected"] is None
        assert (await call(client, auth)).status_code == 503
    assert requests == 2


async def test_stream_cancel_closes_upstream_without_backup(tmp_path):
    from starlette.requests import Request

    from quotamesh.routes.gateway import chat_completions

    stream = Events(FIRST, 100.0, DONE)
    app, client, auth = await setup(
        tmp_path,
        lambda req: httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        ),
    )
    await client.aclose()
    body = json.dumps({"model": "qm/default", "stream": True}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"authorization", auth["Authorization"].encode())],
            "app": app,
        },
        receive,
    )
    response = await chat_completions(request)
    assert await anext(response.body_iterator) == FIRST
    task = asyncio.create_task(anext(response.body_iterator))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert app.state.store.recent_attempts()[0]["outcome"] == "CLIENT_CANCELLED"


def test_v1_database_migrates_without_erasing_connection_history_or_paid_uncertainty(tmp_path):
    from pathlib import Path

    from quotamesh.store import Store

    with sqlite3.connect(tmp_path / "quotamesh.db") as conn:
        conn.executescript((Path(__file__).parent / "fixtures/schema_v1.sql").read_text())
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO credentials(id,provider_id,label,plan_type,base_url,fingerprint,created_at,updated_at) VALUES(1,'custom','Legacy','PAID','https://example.com/v1','fingerprint',?,?)",
            (now, now),
        )
        conn.execute("INSERT INTO secrets VALUES(1,'old-secret',NULL)")
        conn.execute("INSERT INTO project_profiles(id,name,slug) VALUES(1,'Default','default')")
        conn.execute("INSERT INTO profile_targets VALUES(1,1,'custom','legacy-model',1)")
        conn.execute(
            "INSERT INTO attempts(ts,request_id,profile_id,attempt_idx,provider_id,model,credential_id,outcome,streamed) VALUES(?,'legacy',1,1,'custom','legacy-model',1,'OK',0)",
            (now,),
        )
    store = Store(tmp_path)
    assert store.connection_summary()["model"] == "legacy-model"
    assert store.runtime_connection()["secret_value"] == "old-secret"
    assert store.recent_attempts()[0]["request_id"] == "legacy"
    assert store.routing_snapshot(datetime.now(UTC))[4]["unknown"]
    Store(tmp_path)  # Reopening must not backfill the same history twice.
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("SELECT unknown_cost FROM daily_usage").fetchone()[0] == 1


async def test_model_permission_block_is_key_specific_and_model_404_skips_pool(tmp_path):
    calls = []

    def handler(req):
        calls.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer fake-1":
            return httpx.Response(403, json={"error": {"message": "model access denied"}})
        return httpx.Response(200, json={"choices": []})

    _, client, auth = await setup(tmp_path, handler)
    async with client:
        assert (await call(client, auth)).status_code == 200
        assert calls == ["Bearer fake-1", "Bearer fake-2"]
        dry = (await client.get("/api/dry-run", headers=auth)).json()
        assert dry["decisions"][0]["skip_reason"] == "degraded"
        assert dry["decisions"][1]["eligible"]


async def test_nonstream_deadline_and_raw_safety_refusal(tmp_path):
    async def slow(req):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"choices": []})

    _, client, auth = await setup(tmp_path / "slow", slow, policy={"nonstream_deadline_s": 0.05})
    async with client:
        assert (await call(client, auth)).status_code == 504
    raw = b'{ "choices": [{"message":{"refusal":"cannot help"}}], "extension": true }'
    app, client, auth = await setup(
        tmp_path / "refusal", lambda req: httpx.Response(200, content=raw)
    )
    async with client:
        response = await call(client, auth)
        assert response.content == raw and response.headers["x-quotamesh-attempts"] == "1"
        assert all(
            c["status"] == "ACTIVE"
            for c in app.state.store.safe_routing(datetime.now(UTC))["credentials"]
        )


async def test_protocol_first_event_and_missing_secret_never_consume_skip_attempts(tmp_path):
    calls = []

    def handler(req):
        calls.append(req.headers["authorization"])
        stream = Events(b"data: not-json\n\n") if len(calls) == 1 else Events(FIRST, DONE)
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    _, client, auth = await setup(
        tmp_path,
        handler,
        keys=[{"secret_value": "", "env_name": "QUOTAMESH_TEST_MISSING_KEY"}, {}, {}],
        targets=[
            {"provider_id": "custom", "model": "bad"},
            {"provider_id": "custom", "model": "good"},
        ],
    )
    async with client:
        response = await call(client, auth, stream=True)
        assert response.status_code == 200 and response.headers["x-quotamesh-attempts"] == "2"
    assert calls == ["Bearer fake-2", "Bearer fake-2"]


@pytest.mark.parametrize(
    ("status", "error", "headers", "state"),
    [
        (400, {"message": "API key not valid"}, {}, "INVALID"),
        (429, {"message": "Quota exceeded: requests per day"}, {"Retry-After": "120"}, "EXHAUSTED"),
    ],
)
async def test_gemini_body_failure_persists_correct_scope(tmp_path, status, error, headers, state):
    calls = []

    def handler(req):
        calls.append(req)
        return (
            httpx.Response(status, json={"error": error}, headers=headers)
            if len(calls) == 1
            else httpx.Response(200, json={"choices": []})
        )

    app, client, auth = await setup(
        tmp_path,
        handler,
        keys=[{"provider_id": "gemini"}, {"provider_id": "gemini"}],
        targets=[{"provider_id": "gemini", "model": "test-model"}],
    )
    async with client:
        assert (await call(client, auth)).status_code == 200
    routing = app.state.store.safe_routing(datetime.now(UTC))
    if state == "INVALID":
        assert routing["credentials"][0]["status"] == "INVALID"
        assert routing["credentials"][1]["status"] == "ACTIVE"
    else:
        assert (
            next(q for q in routing["quota_states"] if q["quota_group"] == "gemini:account-1")[
                "status"
            ]
            == state
        )


async def test_404_skips_same_model_pool_and_moves_target(tmp_path):
    calls = []

    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append(model)
        return (
            httpx.Response(404, json={"error": {"message": "model not found"}})
            if model == "missing"
            else httpx.Response(200, json={"choices": []})
        )

    _, client, auth = await setup(
        tmp_path,
        handler,
        targets=[
            {"provider_id": "custom", "model": "missing"},
            {"provider_id": "custom", "model": "exists"},
        ],
    )
    async with client:
        assert (await call(client, auth)).status_code == 200
    assert calls == ["missing", "exists"]
