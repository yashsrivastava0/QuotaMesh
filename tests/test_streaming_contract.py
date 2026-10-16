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
