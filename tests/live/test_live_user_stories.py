"""Live user stories S1-S8 and S10 against real OpenAI and Gemini (opt-in, budgeted).

Each test maps to a story in docs/user-stories.md. Docstrings name the live-readiness
features (docs/live-readiness-contracts.md) the story depends on, and the provider calls
it spends. Requests are token-capped by CountingTransport; failures report only statuses,
error codes and descriptions, never prompts, completions or keys.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import httpx
import pytest

from quotamesh.app import create_app

from .helpers import (
    PROMPT,
    PROMPT_TEXT,
    REQUEST_ID,
    TOOLS,
    Checks,
    CountingTransport,
    check_route_headers,
    close_store,
    contains_any,
    expected_cost,
    files_contain,
    live_data_dir,
    lookup_order,
    openai_extra,
    parse_sse,
    serve,
)

pytestmark = pytest.mark.live

PRICES = {"input_price": 1.0, "output_price": 4.0}  # deliberately high USD per million tokens
DAILY_CAP = 0.05


def priced(provider: str, model: str, credential: int) -> dict:
    return {"provider_id": provider, "model": model, "credential_id": credential, **PRICES}


async def test_s1_import_env_file_as_references(
    gateway, live_environment, openai_key, gemini_key, monkeypatch
):
    """S1 - A new user previews a .env file, then imports Gemini (FREE) and OpenAI (PAID).

    Needs: POST /api/environment/parse and /api/environment/import-file ("Environment
    import" contract). Provider calls: none.
    """
    checks = Checks()
    content = live_environment.path.read_text(encoding="utf-8-sig")
    values = live_environment.values()
    parsed = await gateway.api("POST", "/api/environment/parse", json={"content": content})
    checks(not contains_any(json.dumps(parsed), values), "parse response never echoes key values")
    new = {}
    for entry in parsed.get("entries", []):
        if entry.get("status") == "new":
            new.setdefault(entry.get("provider_id"), entry)
    gemini, openai = new.get("gemini"), new.get("openai")
    assert gemini and openai, "parse did not report new Gemini and OpenAI keys"
    checks(gemini.get("suggested_plan") == "FREE", "Gemini is suggested as FREE")
    checks(openai.get("suggested_plan") == "PAID", "OpenAI is suggested as PAID")
    for entry in (gemini, openai):
        secret = live_environment.entries[entry["name"]].value
        checks(entry.get("fingerprint") == secret.fingerprint, f"{entry['name']} fingerprint")
        checks(entry.get("length") == len(secret), f"{entry['name']} value length")
        # The reference resolves only if the server process has the variable.
        monkeypatch.setenv(entry["name"], secret.reveal())

    imported = await gateway.api(
        "POST",
        "/api/environment/import-file",
        json={
            "content": content,
            "store": "reference",
            "items": [
                {
                    "name": gemini["name"],
                    "plan_type": "FREE",
                    "label": "Gemini (live)",
                    "quota_group": "gemini-live",
                },
                {
                    "name": openai["name"],
                    "plan_type": "PAID",
                    "label": "OpenAI (live)",
                    "quota_group": "openai-live",
                },
            ],
        },
        expected=(201,),
    )
    rows = {row.get("name"): row for row in imported.get("imported", [])}
    assert set(rows) == {gemini["name"], openai["name"]}, "both keys were imported"
    checks(rows[gemini["name"]].get("plan_type") == "FREE", "Gemini imported as FREE")
    checks(rows[openai["name"]].get("plan_type") == "PAID", "OpenAI imported as PAID")
    checks(not contains_any(json.dumps(imported), values), "import response never echoes keys")

    wallet = await gateway.api("GET", "/api/wallet")
    free = [g for g in wallet["buckets"]["FREE"] if g["provider_id"] == "gemini"]
    paid = [g for g in wallet["buckets"]["PAID"] if g["provider_id"] == "openai"]
    checks(free, "wallet lists Gemini under FREE")
    checks(paid, "wallet lists OpenAI under PAID")
    keys = [key for group in free + paid for key in group["keys"]]
    checks(
        {key.get("env_name") for key in keys} == {gemini["name"], openai["name"]},
        "credentials are environment references",
    )
    checks(all(key.get("secret_available") for key in keys), "references resolve in the server")
    checks(not contains_any(json.dumps(wallet), values), "wallet never returns key values")
    checks(not files_contain(gateway.data_dir, values), "data directory holds no key values")
    checks(gateway.transport.posts == 0, "import made no provider call")
    checks.verify()


@pytest.mark.parametrize("provider", ["gemini", "openai"])
async def test_s2_doctor_lists_models(gateway, request, provider):
    """S2 - Doctor lists the models a key can see, without generating anything.

    Needs: listing ids without the registry model_list_prefix (Gemini "models/") and
    `skipped_ids` ("Doctor" contract). Provider calls: one free GET /models.
    """
    key = request.getfixturevalue(f"{provider}_key")
    model = request.getfixturevalue(f"{provider}_model")
    plan = "FREE" if provider == "gemini" else "PAID"
    credential = await gateway.add_reference(provider, key, plan)
    result = await gateway.api(
        "POST", "/api/doctor", json={"credential_id": credential, "mode": "models"}
    )
    assert result["outcome"] == "listing_ok", f"listing outcome was {result['outcome']}"
    checks = Checks()
    ids = result.get("models") or []
    checks(ids, "listing returned model ids")
    checks(result.get("models_source") == "PROVIDER", "listing source is PROVIDER")
    checks(gateway.transport.posts == 0, "listing made no chat call")
    checks(model in ids, f"listing includes the chosen model {model}")
    checks(not any(i.startswith("models/") for i in ids), "ids carry no models/ prefix")
    checks(isinstance(result.get("skipped_ids"), int), "listing reports skipped_ids")
    checks.verify()


async def test_s3_gemini_through_default_profile(
    gateway, gemini_key, gemini_model, provider_health
):
    """S3 - The free Gemini default serves a non-stream and a stream request.

    Needs: Gemini stream-usage injection (X-QuotaMesh-Usage-Injected, usage on the attempt),
    the [DONE]-or-finish rule and GET /api/requests/{id}. Provider calls: 2 Gemini.
    """
    provider_health.require("gemini")
    credential = await gateway.add_reference("gemini", gemini_key, "FREE")
    await gateway.setup_default(credential, gemini_model)
    checks = Checks()

    response = await gateway.chat(
        {"model": "qm/default", "messages": PROMPT, "max_completion_tokens": 32}
    )
    await gateway.ensure_served(response, "gemini", provider_health)
    check_route_headers(
        checks, response, profile="default", provider="gemini", model=gemini_model, attempts=1
    )
    message = response.json()["choices"][0]["message"]
    checks(message.get("content"), "non-stream reply has content")
    attempt = ((await gateway.trace(response, checks)).get("attempts") or [{}])[-1]
    checks(attempt.get("outcome") == "OK", "non-stream attempt outcome is OK")
    checks((attempt.get("input_tokens") or 0) > 0, "non-stream attempt recorded input tokens")

    response = await gateway.chat(
        {"model": "qm/default", "messages": PROMPT, "max_completion_tokens": 32, "stream": True}
    )
    await gateway.ensure_served(response, "gemini", provider_health)
    check_route_headers(checks, response, profile="default", provider="gemini", attempts=1)
    stream = parse_sse(response.text)
    checks(stream.done, "stream ended with [DONE]")
    checks(not stream.errors, "stream carried no error event")
    checks(stream.text, "stream delivered content")
    checks(all(chunk.get("choices") for chunk in stream.chunks), "no choices-less chunk delivered")
    checks(
        response.headers.get("x-quotamesh-usage-injected") == "true",
        "X-QuotaMesh-Usage-Injected is true",
    )
    attempt = ((await gateway.trace(response, checks)).get("attempts") or [{}])[-1]
    checks(attempt.get("outcome") == "OK", "stream attempt is OK, not MID_STREAM_FAILURE")
    checks(attempt.get("streamed") == 1, "stream attempt is marked streamed")
    checks((attempt.get("input_tokens") or 0) > 0, "stream attempt recorded usage")
    checks.verify()


def test_s4_openai_sdk_compatibility(
    live_budget, live_environment, openai_key, openai_model, provider_health
):
    """S4 - An unmodified OpenAI SDK app works through a real QuotaMesh server.

    Needs: `created` on /v1/models items, GET /v1/models/{id}, OpenAI stream-usage
    injection with the usage-only chunk withheld, GET /api/requests/{id}. The SDK create,
    raw headers, tools round-trip and JSON mode work on the base gateway.
    Provider calls: 5 OpenAI (create, two tool turns, JSON mode, stream).
    """
    openai = pytest.importorskip("openai")
    provider_health.require("openai")
    extra = openai_extra(openai_model)
    checks = Checks()
    with live_data_dir() as directory:
        transport = CountingTransport(httpx.AsyncHTTPTransport(), live_budget)
        app = create_app(directory, transport)
        auth = {"Authorization": "Bearer " + app.state.local_key}
        with (
            serve(app) as origin,
            httpx.Client(base_url=origin, headers=auth, trust_env=False, timeout=60) as admin,
        ):
            created = admin.post(
                "/api/credentials",
                json={
                    "provider_id": "openai",
                    "plan_type": "PAID",
                    "label": "OpenAI live key",
                    "env_name": openai_key,
                },
            )
            assert created.status_code == 201, f"credential: HTTP {created.status_code}"
            body = {
                "slug": "sdk",
                "name": "SDK app",
                "targets": [priced("openai", openai_model, created.json()["credential_id"])],
                "allow_paid": True,
                "paid_daily_cap_usd": DAILY_CAP,
                "nonstream_deadline_s": 90,
                "first_event_timeout_s": 45,
            }
            saved = admin.post("/api/profiles", json=body)
            assert saved.status_code == 201, f"profile: HTTP {saved.status_code}"

            def trace(request_id: str) -> dict:
                # A stream's attempt is recorded just after its last byte; allow a moment.
                for _ in range(20):
                    found = admin.get(f"/api/requests/{request_id}")
                    if found.status_code == 200:
                        return found.json()
                    time.sleep(0.1)
                checks(False, "GET /api/requests/{id} returns the trace")
                return {}

            client = openai.OpenAI(
                base_url=origin + "/v1", api_key=app.state.local_key, max_retries=0, timeout=120
            )

            def sdk_story() -> None:
                listing = client.models.list()
                ids = [model.id for model in listing.data]
                checks("qm/sdk" in ids, "models.list() includes qm/sdk")
                checks(
                    all(isinstance(getattr(m, "created", None), int) for m in listing.data),
                    "model items carry an integer created",
                )
                checks(admin.get("/v1/models/qm/sdk").status_code == 200, "GET /v1/models/{id}")
                missing = admin.get("/v1/models/qm/no-such-profile")
                checks(missing.status_code == 404, "unknown model id returns 404")

                raw = client.chat.completions.with_raw_response.create(
                    model="qm/sdk", messages=PROMPT, max_completion_tokens=16, **extra
                )
                completion = raw.parse()
                checks(raw.headers.get("x-quotamesh-provider") == "openai", "raw provider header")
                checks(raw.headers.get("x-quotamesh-attempts") == "1", "raw attempts header")
                checks(
                    REQUEST_ID.fullmatch(raw.headers.get("x-quotamesh-request-id", "")),
                    "raw request-id header",
                )
                checks(completion.choices[0].message.content, "create() returned content")
                checks(completion.usage and completion.usage.prompt_tokens > 0, "create() usage")

                messages = [
                    {"role": "user", "content": "What is the status of order A-17? Use the tool."}
                ]
                raw = client.chat.completions.with_raw_response.create(
                    model="qm/sdk",
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="required",
                    max_completion_tokens=64,
                    **extra,
                )
                message = raw.http_response.json()["choices"][0]["message"]
                calls = message.get("tool_calls") or []
                assert calls, "the model made no tool call"
                messages.append(message)  # echoed exactly as received
                for call in calls:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                    result = lookup_order(arguments.get("order_id", ""))
                    messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
                    )
                final = client.chat.completions.create(
                    model="qm/sdk",
                    messages=messages,
                    tools=TOOLS,
                    max_completion_tokens=64,
                    **extra,
                )
                checks(final.choices[0].message.content, "answer after the tool result")

                result = client.chat.completions.create(
                    model="qm/sdk",
                    messages=[
                        {"role": "system", "content": "Reply with a JSON object only."},
                        {"role": "user", "content": 'Return {"ok": true} as JSON.'},
                    ],
                    response_format={"type": "json_object"},
                    max_completion_tokens=32,
                    **extra,
                )
                try:
                    document = json.loads(result.choices[0].message.content or "")
                except ValueError:
                    document = None
                checks(isinstance(document, dict), "json_object mode returned a JSON object")

                # Last: without usage injection a capped profile cannot price this stream, and
                # it would then fail closed (unknown_spend) for every later paid request.
                with client.chat.completions.with_streaming_response.create(
                    model="qm/sdk", messages=PROMPT, max_completion_tokens=16, stream=True, **extra
                ) as streamed:
                    stream_headers = streamed.headers
                    chunks = list(streamed.parse())
                text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
                checks(text, "stream delivered text")
                checks(all(chunk.choices for chunk in chunks), "no usage-only chunk was delivered")
                checks(
                    stream_headers.get("x-quotamesh-usage-injected") == "true",
                    "X-QuotaMesh-Usage-Injected is true",
                )
                attempt = (
                    trace(stream_headers.get("x-quotamesh-request-id", "")).get("attempts") or [{}]
                )[-1]
                checks((attempt.get("input_tokens") or 0) > 0, "stream attempt recorded usage")

            failure = None
            try:
                sdk_story()
            except openai.APIStatusError as exc:
                # Keep only status and code: upstream error text can quote masked key fragments.
                failure = (exc.status_code, exc.code)
            finally:
                client.close()
            if failure is not None:
                if failure[1] == "insufficient_quota":
                    provider_health.disable("openai", "OpenAI reported insufficient_quota")
                pytest.fail(f"OpenAI SDK call failed: HTTP {failure[0]}, code {failure[1]}")
        close_store(app)  # the lifespan normally does this; harmless when already closed
        checks(transport.posts == 5, f"exactly five OpenAI calls were made ({transport.posts})")
        checks(not files_contain(directory, live_environment.values()), "no key in data files")
    checks.verify()


async def test_s5_paid_guard_makes_no_upstream_call(gateway, openai_key, openai_model):
    """S5 - Paid access stays blocked until a profile explicitly allows it.

    Works on the base gateway; recovery `href` on Explain needs the integrated contract.
    Provider calls: none (the assertion is that none happen).
    """
    credential = await gateway.add_reference("openai", openai_key, "PAID")
    target = {"provider_id": "openai", "model": openai_model, "credential_id": credential}
    await gateway.create_profile("paid-guard", [target])
    before = gateway.budget.count("openai")
    for stream in (False, True):
        response = await gateway.chat(
            {
                "model": "qm/paid-guard",
                "messages": PROMPT,
                "max_completion_tokens": 16,
                "stream": stream,
            }
        )
        assert response.status_code == 403, f"expected 403, got {response.status_code}"
        assert response.json()["error"]["code"] == "paid_blocked"
    assert gateway.transport.posts == 0 and gateway.budget.count("openai") == before
    checks = Checks()
    decision = (await gateway.api("GET", "/api/explain?profile=paid-guard"))["decisions"][0]
    checks(decision["skip_reason"] == "paid_blocked", "Explain reports paid_blocked")
    action = decision.get("recovery_action") or {}
    checks(action.get("href"), "recovery action links to the page that fixes it")
    checks.verify()


async def test_s6_paid_cap_prices_then_blocks(gateway, openai_key, openai_model, provider_health):
    """S6 - Allowed paid use is priced locally and stops at the daily cap.

    Needs: GET /api/requests/{id}; blocked responses with X-QuotaMesh-Profile and
    X-QuotaMesh-Request-Id plus candidate explanations. Provider calls: 1 OpenAI.
    """
    provider_health.require("openai")
    credential = await gateway.add_reference("openai", openai_key, "PAID")
    target = priced("openai", openai_model, credential)
    await gateway.create_profile("capped", [target], allow_paid=True, paid_daily_cap_usd=DAILY_CAP)
    payload = {
        "model": "qm/capped",
        "messages": PROMPT,
        "max_completion_tokens": 16,
        **openai_extra(openai_model),
    }
    checks = Checks()
    response = await gateway.chat(payload)
    await gateway.ensure_served(response, "openai", provider_health)
    cost = expected_cost(response.json().get("usage") or {}, **PRICES)
    checks(cost > 0, "provider usage allows a local cost estimate")
    trace = await gateway.trace(response, checks)
    attempt = (trace.get("attempts") or [{}])[-1]
    estimated = attempt.get("estimated_cost_usd") or 0
    checks(abs(estimated - cost) < 1e-9, "attempt cost matches the manual prices")
    checks(trace.get("totals", {}).get("cost_source") == "LOCAL", "trace cost source is LOCAL")

    explain = await gateway.api("GET", "/api/explain?profile=capped")
    observed = explain["caps"]["daily"]["observed_usd"]
    assert observed > 0, "observed daily paid spend was not recorded"
    await gateway.update_profile(
        "capped", [target], allow_paid=True, paid_daily_cap_usd=observed / 2
    )
    calls = gateway.transport.posts
    blocked = await gateway.chat(payload)
    assert blocked.status_code in (429, 503), f"expected a block, got {blocked.status_code}"
    body = blocked.json()
    checks(body["error"]["code"] == "no_candidates", "blocked with no_candidates")
    candidates = body.get("quotamesh", {}).get("candidates") or [{}]
    checks(candidates[0].get("skip_reason") == "cap_reached", "candidate skipped as cap_reached")
    checks(candidates[0].get("explanation"), "candidate carries an explanation")
    checks(candidates[0].get("recovery_action"), "candidate carries a recovery action")
    checks(blocked.headers.get("x-quotamesh-profile") == "capped", "blocked response names profile")
    checks(
        REQUEST_ID.fullmatch(blocked.headers.get("x-quotamesh-request-id", "")),
        "blocked response carries a request id",
    )
    checks(gateway.transport.posts == calls, "no upstream call after the cap was reached")
    checks.verify()


async def test_s7_rejected_gemini_key_falls_back(
    gateway, gemini_key, gemini_model, provider_health, monkeypatch
):
    """S7 - A rejected key is marked INVALID and the next key in the pool serves.

    Needs: classification of Gemini's root-array error bodies (bad key -> auth) and
    GET /api/requests/{id}. Provider calls: 1 rejected (free) + 1 Gemini generation.
    """
    provider_health.require("gemini")
    monkeypatch.setenv("QM_LIVE_BOGUS_GEMINI_KEY", "quotamesh-live-invalid-gemini-key")
    bogus = await gateway.add_reference(
        "gemini",
        "QM_LIVE_BOGUS_GEMINI_KEY",
        "FREE",
        label="Gemini rejected key",
        quota_group="gemini-bogus",
        priority=-1,
    )
    await gateway.add_reference(
        "gemini", gemini_key, "FREE", label="Gemini live key", quota_group="gemini-live"
    )
    await gateway.create_profile("gemini-pool", [{"provider_id": "gemini", "model": gemini_model}])
    response = await gateway.chat(
        {"model": "qm/gemini-pool", "messages": PROMPT, "max_completion_tokens": 32}
    )
    await gateway.ensure_served(response, "gemini", provider_health)
    checks = Checks()
    check_route_headers(checks, response, profile="gemini-pool", provider="gemini", attempts=2)
    checks(response.headers.get("x-quotamesh-key") == "Gemini live key", "live key served")
    attempts = (await gateway.trace(response, checks)).get("attempts") or [{}, {}]
    checks(attempts[0].get("error_class") == "auth", "first attempt classified as auth")
    checks(attempts[-1].get("outcome") == "OK", "second attempt succeeded")
    status = await gateway.api("GET", "/api/status?profile=gemini-pool")
    rejected = next(c for c in status["routing"]["credentials"] if c["id"] == bogus)
    checks(rejected["status"] == "INVALID", "rejected key is marked INVALID")
    checks.verify()


async def test_s8_unknown_model_falls_back_to_paid_openai(
    gateway, gemini_key, openai_key, openai_model, provider_health
):
    """S8 - An unknown Gemini model on target 1 falls back to capped OpenAI on target 2.

    Needs: GET /api/requests/{id}. Provider calls: 1 rejected Gemini (free) + 1 OpenAI.
    """
    provider_health.require("openai")
    gemini = await gateway.add_reference("gemini", gemini_key, "FREE")
    openai = await gateway.add_reference("openai", openai_key, "PAID")
    targets = [
        {
            "provider_id": "gemini",
            "model": "gemini-quotamesh-live-no-such-model",
            "credential_id": gemini,
        },
        priced("openai", openai_model, openai),
    ]
    await gateway.create_profile(
        "cross-provider", targets, allow_paid=True, paid_daily_cap_usd=DAILY_CAP
    )
    # No reasoning_effort here: the same body reaches Gemini first.
    tokens = 64 if openai_model.startswith("gpt-5") else 16
    response = await gateway.chat(
        {"model": "qm/cross-provider", "messages": PROMPT, "max_completion_tokens": tokens}
    )
    await gateway.ensure_served(response, "openai", provider_health)
    checks = Checks()
    check_route_headers(
        checks,
        response,
        profile="cross-provider",
        provider="openai",
        model=openai_model,
        attempts=2,
    )
    attempts = (await gateway.trace(response, checks)).get("attempts") or [{}, {}]
    checks(attempts[0].get("provider_id") == "gemini", "first attempt went to Gemini")
    checks(attempts[0].get("http_status") == 404, "Gemini answered 404 for the unknown model")
    checks(attempts[0].get("error_class") == "model_access", "404 classified as model_access")
    checks(attempts[-1].get("outcome") == "OK", "OpenAI served the request")
    checks((attempts[-1].get("estimated_cost_usd") or 0) > 0, "OpenAI attempt was priced")
    checks.verify()


async def test_s10_usage_and_activity_accumulate(
    gateway, gemini_key, gemini_model, openai_key, openai_model, provider_health
):
    """S10 - Usage, activity and traces accumulate across providers.

    Needs: wallet `daily` (14 days with by_provider), Gemini stream-usage injection and
    GET /api/requests/{id}. Provider calls: 1 Gemini stream + 1 OpenAI.
    """
    provider_health.require("gemini")
    provider_health.require("openai")
    gemini = await gateway.add_reference("gemini", gemini_key, "FREE")
    openai = await gateway.add_reference("openai", openai_key, "PAID")
    await gateway.setup_default(gemini, gemini_model)
    await gateway.create_profile(
        "paid-app",
        [priced("openai", openai_model, openai)],
        allow_paid=True,
        paid_daily_cap_usd=DAILY_CAP,
    )
    first = await gateway.chat(
        {"model": "qm/default", "messages": PROMPT, "max_completion_tokens": 32, "stream": True}
    )
    await gateway.ensure_served(first, "gemini", provider_health)
    second = await gateway.chat(
        {
            "model": "qm/paid-app",
            "messages": PROMPT,
            "max_completion_tokens": 16,
            **openai_extra(openai_model),
        }
    )
    await gateway.ensure_served(second, "openai", provider_health)
    checks = Checks()
    wallet = await gateway.api("GET", "/api/wallet")
    today = wallet["today"]
    checks(today["routed_requests"] == 2, "wallet today counts two routed requests")
    checks(today["attempts"] == 2, "wallet today counts two attempts")
    checks((today["input_tokens"] or 0) > 0, "wallet today has input tokens")
    checks((today["output_tokens"] or 0) > 0, "wallet today has output tokens")
    checks(today["known_cost_usd"] > 0, "wallet today has known cost from the priced call")
    daily = wallet.get("daily") or []
    checks(len(daily) == 14, "wallet daily series has 14 days")
    last = daily[-1] if daily else {}
    checks(last.get("day") == datetime.now(UTC).date().isoformat(), "daily ends today (UTC)")
    checks(last.get("routed_requests") == 2, "daily today counts two requests")
    by_provider = last.get("by_provider") or {}
    checks(
        (by_provider.get("gemini") or {}).get("routed_requests") == 1
        and (by_provider.get("openai") or {}).get("routed_requests") == 1,
        "daily by_provider splits Gemini and OpenAI",
    )
    checks((by_provider.get("openai") or {}).get("known_cost_usd", 0) > 0, "OpenAI cost by day")

    activity = await gateway.api("GET", "/api/activity?limit=10")
    request_ids = {r.headers.get("x-quotamesh-request-id") for r in (first, second)}
    listed = {item["request_id"] for item in activity["requests"]}
    checks(request_ids <= listed, "activity lists both requests")
    traces = []
    for response in (first, second):
        trace = await gateway.trace(response, checks)
        traces.append(trace)
        totals = trace.get("totals") or {}
        checks(totals.get("attempts") == 1, "trace totals count one attempt")
        checks((totals.get("input_tokens") or 0) > 0, "trace totals include input tokens")
    history = json.dumps(activity) + json.dumps(traces)
    checks(PROMPT_TEXT not in history, "history holds no prompt text")
    checks.verify()
