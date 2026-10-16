"""Gateway contract: first-event errors, stream usage, stream end and deadlines."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_phase_two import FIRST, Events, call, setup

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
