"""Scripted OpenAI-shaped provider. No real secrets, accounts, or external requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="QuotaMesh fake upstream")


@app.get("/v1/models")
async def models(request: Request):
    key = request.headers.get("authorization", "").removeprefix("Bearer ").split(":", 1)[0]
    if key == "fake-401":
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
    if key == "fake-models-unsupported":
        return JSONResponse({"error": {"message": "not supported"}}, status_code=404)
    if key == "fake-models-malformed":
        return {"unexpected": True}
    if key == "fake-models-timeout":
        await asyncio.sleep(11)
    if key == "fake-models-redirect":
        return JSONResponse({}, status_code=302, headers={"Location": "https://example.com"})
    if not key.startswith("fake-"):
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
    return {
        "object": "list",
        "data": [{"id": model, "object": "model"} for model in ("fake-free", "demo-billable")],
    }


@app.post("/v1/chat/completions")
async def chat(request: Request):
    key = request.headers.get("authorization", "").removeprefix("Bearer ").split(":", 1)[0]
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
        "fake-paid-unknown",
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
                        "usage": {}
                        if key == "fake-paid-unknown"
                        else {
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
        "usage": {}
        if key == "fake-paid-unknown"
        else {
            "prompt_tokens": 1,
            "completion_tokens": 4,
            **({"cost_usd": 0.6} if key == "fake-paid" else {}),
        },
    }


SCENARIOS = {
    "empty": ("First run — no API access", "fake-200", "fake-200", False, False),
    "unconfigured": ("Access saved — no profile yet", "fake-200", "fake-200", False, False),
    "success": ("Successful request — one target", "fake-200", "fake-200", False, False),
    "expired": ("Expired trial — free backup", "fake-200", "fake-200", False, False),
    "invalid": ("Rejected key — free backup", "fake-401", "fake-200", False, False),
    "missing-env": (
        "Missing environment reference — free backup",
        "fake-200",
        "fake-200",
        False,
        False,
    ),
    "unknown-cost": (
        "Unknown paid cost — later requests blocked",
        "fake-429-short",
        "fake-429-short",
        True,
        False,
    ),
    "accounting": (
        "Incomplete accounting — paid blocked",
        "fake-429-short",
        "fake-429-short",
        True,
        False,
    ),
    "large": ("Long labels and many sources", "fake-200", "fake-200", False, False),
    "wallet": ("Mixed wallet + named profiles", "fake-429-short", "fake-200", False, False),
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


GUIDES = {
    "wallet": "Review free, trial, and paid access. Two trial keys share one $20 balance. Choose paid-backup to observe a simulated $0.60 request.",
    "fallback": "Send a request. The primary is rate limited; the free backup succeeds. Expect two attempts. The next request skips the cooling primary.",
    "paid-guard": "Send a request. Both free targets fail; paid stays blocked. Expect two attempts and no paid call.",
    "paid-cap": "Send two requests to observe $0.60 each. A third paid call is blocked by the $1 UTC daily cap. In-flight requests can overshoot the cap.",
    "shared-quota": "Send a request. One 429 blocks the sibling key sharing its quota group/model. Expect one attempt and no independent capacity from the second key.",
    "error-first": "Use streaming and send a request. The first provider reports an error before output starts; the backup succeeds. Expect two attempts.",
    "midstream": "Use streaming and send a request. One provider starts output, then ends early. Expect an interruption, one attempt, and no provider switch.",
    "empty": "No access is configured. Go to API access and add a synthetic key with the local fake endpoint, then save an exact model in a profile.",
    "unconfigured": "Access is saved, but no profile exists. Go to API access and create a safe default with model fake-free. Setup sends no generation request.",
    "success": "Send a request. The first free target succeeds. Expect one attempt and no fallback.",
    "expired": "Inspect the route. The trial expired yesterday and is skipped. Send a request to observe the free backup in one attempt.",
    "invalid": "Send a request. The primary key is rejected; the backup succeeds in two attempts. Later requests skip the rejected key until it is replaced or reset.",
    "missing-env": "Inspect the route. The primary environment reference is absent from the server process. The free backup succeeds in one attempt.",
    "unknown-cost": "Send a request. Paid fallback reports no usage/cost, so cost stays unknown. Send again: capped paid routing blocks because observed spend is incomplete.",
    "accounting": "Inspect the route. Paid accounting is incomplete, so paid stays blocked. Repair storage and reconcile missing spend before using paid access.",
    "large": "Review 38 sources with long Unicode labels at desktop and mobile sizes. The first free target succeeds in one attempt.",
}


def configure_demo(server, base_url, scenario="fallback"):
    if scenario not in SCENARIOS:
        raise ValueError("Unknown demo scenario")
    _, first, second, paid, shared = SCENARIOS[scenario]
    server.state.demo_scenario = scenario
    store = server.state.store
    # Only the CLI's isolated temporary demo can call this function.
    with store.connection() as conn:
        conn.execute("DELETE FROM profile_targets")
        conn.execute("DELETE FROM project_profiles")
        conn.execute("DELETE FROM usage_rollups")
        conn.execute("DELETE FROM credential_checks")
        conn.execute("DELETE FROM rate_observations")
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
            secret_value=secret + ":" + hashlib.sha256(label.encode()).hexdigest()[:8],
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
                "model": "demo-billable",
                "credential_id": 3,
                "input_price": 1,
                "output_price": 1,
            },
        ],
    )

    if scenario in {"empty", "unconfigured"}:
        with store.connection() as conn:
            conn.execute("DELETE FROM profile_targets")
            conn.execute("DELETE FROM project_profiles")
            if scenario == "empty":
                conn.execute("DELETE FROM credentials")
    elif scenario == "expired":
        store.edit_credential(
            1,
            {
                "plan_type": "TRIAL_CREDIT",
                "trial_expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            },
        )
    elif scenario == "missing-env":
        store.edit_credential(1, {"env_name": "QUOTAMESH_DEMO_MISSING_REFERENCE"})
    elif scenario == "unknown-cost":
        store.edit_credential(3, {"secret_value": "fake-paid-unknown"})
    elif scenario == "accounting":
        server.state.accounting_failed = True
    elif scenario == "large":
        for index in range(35):
            store.add_credential(
                provider_id="custom",
                label=f"Demo équipe 日本語 {index + 1} — a longer project label for layout testing",
                plan_type="FREE",
                base_url=base_url,
                secret_value=f"fake-200:large-{index}",
                env_name=None,
                quota_group=f"demo-layout-{index}",
                priority=index,
                trial_expires_at=None,
            )

    if scenario == "wallet":
        for label in ("Hackathon trial", "Hackathon second key — shared credit"):
            store.add_credential(
                provider_id="custom",
                label=label,
                plan_type="TRIAL_CREDIT",
                base_url=base_url,
                secret_value="fake-paid:" + hashlib.sha256(label.encode()).hexdigest()[:8],
                env_name=None,
                quota_group="hackathon",
                account_label="Hackathon project",
                starting_credit_usd=20,
                trial_expires_at=(datetime.now(UTC) + timedelta(days=19)).isoformat(),
            )
        from quotamesh.policy import Policy

        base = Policy(
            targets=[{"provider_id": "custom", "model": "fake-free", "credential_id": 2}]
        ).model_dump()
        free_targets = base.pop("targets")
        store.save_profile(
            "free-app", base | {"allow_trial": False}, free_targets, name="Free app", create=True
        )
        trial_target = {
            "provider_id": "custom",
            "model": "fake-trial",
            "credential_id": 4,
            "input_price": 1,
            "output_price": 1,
        }
        paid_target = {
            "provider_id": "custom",
            "model": "demo-billable",
            "credential_id": 3,
            "input_price": 1,
            "output_price": 1,
        }
        store.save_profile(
            "paid-backup",
            base | {"allow_paid": True, "paid_daily_cap_usd": 1, "paid_monthly_cap_usd": 10},
            [trial_target, paid_target],
            name="Trial with paid backup",
            create=True,
        )
