"""Scripted OpenAI-shaped provider. No real secrets, accounts, or external requests."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="QuotaMesh fake upstream")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    key = request.headers.get("authorization", "").removeprefix("Bearer ")
    payload = await request.json()
    failures = {
        "fake-401": (401, {"message": "fake invalid key"}, {}),
        "fake-400-badkey": (400, {"message": "API key not valid"}, {}),
        "fake-403-model": (403, {"message": "model access denied"}, {}),
        "fake-429": (429, {"message": "fake rate limit"}, {"Retry-After": "5"}),
        "fake-429-short": (429, {"message": "fake rate limit"}, {"Retry-After": "5"}),
        "fake-429-daily": (429, {"message": "daily quota exhausted"}, {"Retry-After": "120"}),
        "fake-429-unknown": (429, {"message": "unknown rate limit"}, {}),
        "fake-5xx": (503, {"message": "overloaded"}, {}),
        "fake-context-too-long": (400, {"code": "context_length_exceeded"}, {}),
    }
    if key in failures:
        status, error, headers = failures[key]
        return JSONResponse({"error": error}, status_code=status, headers=headers)
    allowed = {
        "fake-200",
        "fake-sse",
        "fake-slow-first-event",
        "fake-sse-error-first",
        "fake-midstream-cut",
        "fake-paid",
    }
    if key not in allowed:
        return JSONResponse({"error": {"message": "Unknown fake scenario"}}, status_code=401)
    if payload.get("stream"):

        async def events():
            if key == "fake-slow-first-event":
                await asyncio.sleep(2)
            if key == "fake-sse-error-first":
                yield 'data: {"error":{"code":429,"message":"fake rate limit"}}\n\n'
                return
            # A role-only event commits immediately, before content.
            yield 'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
            if key == "fake-midstream-cut":
                return  # A missing DONE is a detectable interruption, without noisy server errors.
            chunk = {
                "id": "fake-1",
                "object": "chat.completion.chunk",
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hello from fake upstream"},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield "data: " + json.dumps(chunk) + "\n\n"
            yield (
                "data: "
                + json.dumps(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 4,
                            **({"cost_usd": 0.6} if key == "fake-paid" else {}),
                        },
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")
    return {
        "id": "fake-1",
        "object": "chat.completion",
        "model": payload["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from fake upstream"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 4,
            **({"cost_usd": 0.6} if key == "fake-paid" else {}),
        },
    }


SCENARIOS = {
    "fallback": ("Rate limit → free backup", "fake-429-short", "fake-200", False, False),
    "paid-guard": (
        "Both free sources fail → paid blocked",
        "fake-429-short",
        "fake-429-short",
        False,
        False,
    ),
    "paid-cap": ("Paid backup → $1 daily cap", "fake-429-short", "fake-429-short", True, False),
    "shared-quota": ("Shared project → sibling skipped", "fake-429-short", "fake-200", False, True),
    "error-first": (
        "Stream error before commitment → backup",
        "fake-sse-error-first",
        "fake-200",
        False,
        False,
    ),
    "midstream": (
        "Stream interruption → no fallback",
        "fake-midstream-cut",
        "fake-200",
        False,
        False,
    ),
}


def configure_demo(server, base_url, scenario="fallback"):
    if scenario not in SCENARIOS:
        raise ValueError("Unknown demo scenario")
    _, first, second, paid, shared = SCENARIOS[scenario]
    store = server.state.store
    # Only the CLI's isolated temporary demo can call this function.
    with store.connection() as conn:
        conn.execute("DELETE FROM profile_targets")
        conn.execute("DELETE FROM credentials")
        conn.execute("DELETE FROM quota_state")
        conn.execute("DELETE FROM daily_usage")
        conn.execute("DELETE FROM attempts")
    server.state.degraded.clear()
    server.state.accounting_failed = False
    for label, secret, plan, group in [
        ("Primary · simulated free", first, "FREE", "project-a"),
        ("Backup · simulated free", second, "FREE", "project-a" if shared else "project-b"),
        ("Paid · simulated dollars", "fake-paid", "PAID", "paid-project"),
    ]:
        store.add_credential(
            provider_id="custom",
            label=label,
            plan_type=plan,
            base_url=base_url,
            secret_value=secret,
            env_name=None,
            quota_group=group,
            priority=0,
            trial_expires_at=None,
        )
    store.save_policy(
        {
            "allow_paid": int(paid),
            "allow_trial": 1,
            "allow_unknown_price": 0,
            "enabled": 1,
            "max_attempts": 5,
            "nonstream_deadline_s": 45,
            "first_event_timeout_s": 30,
            "paid_daily_cap_usd": 1,
            "paid_monthly_cap_usd": 10,
        },
        [
            {
                "provider_id": "custom",
                "model": "fake-free",
                "credential_id": 1,
                "input_price": None,
                "output_price": None,
            },
            {
                "provider_id": "custom",
                "model": "fake-free",
                "credential_id": 2,
                "input_price": None,
                "output_price": None,
            },
            {
                "provider_id": "custom",
                "model": "fake-paid",
                "credential_id": 3,
                "input_price": 1,
                "output_price": 1,
            },
        ],
    )
