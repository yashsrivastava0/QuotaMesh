"""Gateway contract: first-event errors, stream usage, stream end and deadlines."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_phase_two import FIRST, Events, call, setup

from quotamesh.app import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "providers"
SSE = {"content-type": "text/event-stream"}


def attempts(app):
    with app.state.store.connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT outcome, error_class, http_status, input_tokens, output_tokens, "
                "estimated_cost_usd FROM attempts ORDER BY id"
            )
        ]


@pytest.mark.parametrize(
    ("error", "calls_made", "status", "code"),
    [
        # A Google status string alone maps to 429: cool down and fall back to the next key.
        ({"status": "RESOURCE_EXHAUSTED", "message": "quota"}, 2, 200, None),
        # INVALID_ARGUMENT maps to 400: a bad request is terminal, not a provider failure.
        ({"status": "INVALID_ARGUMENT", "message": "bad field"}, 1, 502, "request_error"),
    ],
)
async def test_first_event_error_status_comes_from_the_error_object(
    tmp_path, error, calls_made, status, code
):
    calls = []

    def handler(request):
        calls.append(request.headers["authorization"])
        event = b"data: " + json.dumps({"error": error}).encode() + b"\n\n"
        stream = Events(event) if len(calls) == 1 else Events(FIRST, b"data: [DONE]\n\n")
        return httpx.Response(200, stream=stream, headers=SSE)

    app, client, auth = await setup(tmp_path, handler)
    async with client:
        response = await call(client, auth, stream=True)
    assert response.status_code == status and len(calls) == calls_made
    if code:
        assert response.json()["error"]["code"] == code
    else:
        states = app.state.store.safe_routing(datetime.now(UTC))["quota_states"]
        assert {s["quota_group"]: s["status"] for s in states}["custom:account-1"] == "COOLDOWN"


def fixture_events(provider, case, separator=b"\n\n"):
    """Rebuild upstream SSE events from a sanitized live capture, one item per event."""
    data = json.loads((FIXTURES / provider / f"{case}.json").read_text(encoding="utf-8"))
    return [
        b"data: "
        + (event if isinstance(event, str) else json.dumps(event["data"])).encode()
        + separator
        for event in data["events"]
    ]


def usage_only(event):
    payload = event.split(b":", 1)[1].strip()
    obj = None if payload == b"[DONE]" else json.loads(payload)
    return isinstance(obj, dict) and obj.get("choices") == [] and isinstance(obj.get("usage"), dict)


def only(provider, model):
    return {
        "keys": [{"provider_id": provider}],
        "targets": [{"provider_id": provider, "model": model}],
    }


async def stream_once(tmp_path, upstream, *, options=None, **request):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, stream=Events(*upstream), headers=SSE)

    app, client, auth = await setup(tmp_path, handler, **(options or {}))
    async with client:
        response = await call(client, auth, stream=True, **request)
    return app, response, seen


def tokens(app):
    row = attempts(app)[-1]
    return row["outcome"], row["input_tokens"], row["output_tokens"]


@pytest.mark.parametrize(
    ("client_options", "forwarded"),
    [
        (None, {"include_usage": True}),
        ({"include_obfuscation": False}, {"include_obfuscation": False, "include_usage": True}),
        ({"include_usage": False}, {"include_usage": True}),
    ],
)
async def test_injected_usage_is_recorded_and_its_usage_only_chunk_hidden(
    tmp_path, client_options, forwarded
):
    events = fixture_events("openai", "stream_usage")
    assert sum(map(usage_only, events)) == 1  # the live capture has one chunk to hide
    request = {} if client_options is None else {"stream_options": client_options}
    app, response, seen = await stream_once(
        tmp_path, events, options=only("openai", "gpt-4.1-nano"), **request
    )
    assert response.status_code == 200
    assert response.headers["x-quotamesh-usage-injected"] == "true"
    assert seen[0]["stream_options"] == forwarded
    assert response.content == b"".join(e for e in events if not usage_only(e))
    assert tokens(app) == ("OK", 14, 1)


async def test_client_requested_usage_passes_through_byte_for_byte(tmp_path):
    events = fixture_events("openai", "stream_usage")
    app, response, seen = await stream_once(
        tmp_path,
        events,
        options=only("openai", "gpt-4.1-nano"),
        stream_options={"include_usage": True},
    )
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert "x-quotamesh-usage-injected" not in response.headers
    assert response.content == b"".join(events)
    assert tokens(app) == ("OK", 14, 1)


async def test_gemini_usage_on_content_chunks_is_recorded_and_unchanged(tmp_path):
    events = fixture_events("gemini", "stream_usage")
    app, response, seen = await stream_once(
        tmp_path, events, options=only("gemini", "gemini-3.5-flash-lite")
    )
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert response.headers["x-quotamesh-usage-injected"] == "true"
    assert response.content == b"".join(events)
    assert tokens(app) == ("OK", 8, 1)


async def test_providers_without_injection_are_never_modified(tmp_path):
    events = fixture_events("openai", "stream_usage")
    app, response, seen = await stream_once(tmp_path, events)  # custom provider
    assert "stream_options" not in seen[0]
    assert "x-quotamesh-usage-injected" not in response.headers
    assert response.content == b"".join(events)
    assert tokens(app) == ("OK", 14, 1)


async def test_crlf_framing_and_split_chunks_while_stripping(tmp_path):
    events = fixture_events("openai", "stream_usage", separator=b"\r\n\r\n")
    raw = b"".join(events)
    pieces = [raw[i : i + 7] for i in range(0, len(raw), 7)]
    app, response, _ = await stream_once(tmp_path, pieces, options=only("openai", "gpt-4.1-nano"))
    assert response.content == b"".join(e for e in events if not usage_only(e))
    assert tokens(app) == ("OK", 14, 1)


async def test_unbounded_event_flushes_raw_and_stops_stripping(tmp_path, monkeypatch):
    monkeypatch.setattr("quotamesh.routes.gateway.MAX_FRAMED_BYTES", 64)
    events = fixture_events("openai", "stream_usage")
    pieces = [events[0], events[1][:100], events[1][100:], *events[2:]]
    app, response, _ = await stream_once(tmp_path, pieces, options=only("openai", "gpt-4.1-nano"))
    # Nothing is lost or reordered; after the flush the usage chunk is simply forwarded.
    assert response.content == b"".join(events)
    assert tokens(app) == ("OK", 14, 1)


@pytest.mark.parametrize(
    ("tail", "outcome", "appended"),
    [
        # A finish_reason followed by a clean close is complete even without [DONE].
        ("finish", "OK", b""),
        ("finish+done", "OK", b""),
        # No finish_reason and no [DONE]: the client is told the stream was cut short.
        ("content", "MID_STREAM_FAILURE", b"event: error"),
        # A finish_reason is not enough when the connection then fails.
        ("finish+cut", "MID_STREAM_FAILURE", b"event: error"),
        # After [DONE] the response is complete; a later transport error changes nothing.
        ("finish+done+cut", "OK", b""),
    ],
)
async def test_stream_end_needs_done_or_finish_reason_with_clean_close(
    tmp_path, tail, outcome, appended
):
    role, content, finish, done = fixture_events("openai", "stream_plain")
    upstream = {
        "finish": [role, content, finish],
        "finish+done": [role, content, finish, done],
        "content": [role, content],
        "finish+cut": [role, content, finish, httpx.ReadError("cut")],
        "finish+done+cut": [role, content, finish, done, httpx.ReadError("cut")],
    }[tail]
    app, response, _ = await stream_once(tmp_path, upstream)
    sent = b"".join(part for part in upstream if isinstance(part, bytes))
    assert response.status_code == 200 and response.content.startswith(sent)
    assert response.content[len(sent) :].startswith(appended)
    assert bool(response.content[len(sent) :]) == bool(appended)
    assert attempts(app)[-1]["outcome"] == outcome


@pytest.mark.parametrize("streamed", [False, True])
async def test_deadline_is_a_contract_504_that_does_not_degrade_the_target(tmp_path, streamed):
    calls = []

    async def handler(request):
        calls.append(request.headers["authorization"])
        if len(calls) == 1:  # key 1 is throttled, so the request falls back to key 2
            return httpx.Response(429, json={"error": {"message": "rate limit"}})
        if len(calls) == 2:  # key 2 is slower than the whole route deadline
            if streamed:
                return httpx.Response(200, stream=Events(5.0, FIRST), headers=SSE)
            await asyncio.sleep(5)
        return httpx.Response(200, json={"choices": []})

    app, client, auth = await setup(
        tmp_path, handler, policy={"nonstream_deadline_s": 0.5, "first_event_timeout_s": 0.5}
    )
    async with client:
        response = await call(client, auth, stream=streamed)
        assert response.status_code == 504
        body = response.json()
        assert body["error"]["code"] == body["error"]["type"] == "deadline_exceeded"
        assert body["quotamesh"] == {
            "attempts": 2,
            "last_error_class": "deadline",
            "last_http_status": 504,
        }
        assert response.headers["x-quotamesh-attempts"] == "2"
        assert response.headers["x-quotamesh-key"] == "Key 2"
        assert response.headers["x-quotamesh-profile"] == "default"
        assert len(response.headers["x-quotamesh-request-id"]) == 32
        # The deadline says nothing about key 2's target: the next request may use it at once.
        retry = await call(client, auth)
        assert retry.status_code == 200 and retry.headers["x-quotamesh-key"] == "Key 2"
    assert calls == ["Bearer fake-1", "Bearer fake-2", "Bearer fake-2"]
    cut = attempts(app)[1]
    assert (cut["error_class"], cut["http_status"]) == ("deadline", 504)
    assert cut["estimated_cost_usd"] is None  # a cut-off call may have been billed: unknown


async def test_httpx_timeouts_remain_network_failures_that_degrade(tmp_path):
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "slow":
            raise httpx.ReadTimeout("synthetic")
        return httpx.Response(200, json={"choices": []})

    app, client, auth = await setup(
        tmp_path,
        handler,
        keys=[{}],
        targets=[{"provider_id": "custom", "model": m} for m in ("slow", "working")],
    )
    async with client:
        assert (await call(client, auth)).status_code == 200
        dry = (await client.get("/api/dry-run", headers=auth)).json()
        assert dry["decisions"][0]["skip_reason"] == "degraded"
    assert calls == ["slow", "working"]
    assert attempts(app)[0]["error_class"] == "network"


@pytest.mark.parametrize("stalled", [True, False])
async def test_idle_timeout_bounds_each_read_after_commit(tmp_path, monkeypatch, stalled):
    monkeypatch.setattr("quotamesh.routes.gateway.MIN_IDLE_TIMEOUT_S", 0.01)
    role, content, finish, done = fixture_events("openai", "stream_plain")
    # Idle limit per read = max(MIN_IDLE_TIMEOUT_S, first_event_timeout_s) = 0.25 s. The steady
    # stream takes longer than that in total, but never waits that long for one read.
    parts = (
        (role, 5.0, content, finish, done)
        if stalled
        else (role, 0.1, content, 0.1, finish, 0.1, done)
    )
    stream = Events(*parts)
    app, client, auth = await setup(
        tmp_path,
        lambda request: httpx.Response(200, stream=stream, headers=SSE),
        policy={"first_event_timeout_s": 0.25},
    )
    async with client:
        response = await call(client, auth, stream=True)
    assert response.status_code == 200 and response.content.startswith(role)
    assert (b"Upstream stream interrupted" in response.content) is stalled
    assert (done in response.content) is not stalled
    assert attempts(app)[-1]["outcome"] == ("MID_STREAM_FAILURE" if stalled else "OK")
    assert stream.closed


async def test_reads_are_bounded_by_profile_deadlines_not_httpx(tmp_path):
    app = create_app(tmp_path)
    timeout = app.state.client.timeout
    await app.state.client.aclose()
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5, None, 10, 5)


async def test_stream_completes_at_done_without_waiting_for_upstream_close(tmp_path):
    """SDKs disconnect right after [DONE]; that must be recorded as OK, not cancelled."""
    import time as _time

    class HangsAfterDone(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"
            await asyncio.sleep(30)  # an upstream that keeps the connection open

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=HangsAfterDone()
        )

    app, client, auth = await setup(tmp_path, handler, keys=[{}])
    async with client:
        started = _time.monotonic()
        response = await call(client, auth, stream=True)
        assert response.status_code == 200
        assert response.content.endswith(b"data: [DONE]\n\n")
        assert _time.monotonic() - started < 5
    [attempt] = app.state.store.recent_attempts()
    assert attempt["outcome"] == "OK" and attempt["error_class"] is None
