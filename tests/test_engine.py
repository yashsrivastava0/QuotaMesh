"""PDF sections 7–12: deterministic selection and body-aware state fixtures."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from quotamesh.engine.classify import classify, next_quota_state, retry_after, usage_metadata
from quotamesh.engine.select import candidates

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
PROFILE = {
    "enabled": 1,
    "allow_trial": 1,
    "allow_paid": 0,
    "allow_unknown_price": 0,
    "paid_daily_cap_usd": None,
    "paid_monthly_cap_usd": None,
}
KEY = {
    "id": 1,
    "provider_id": "custom",
    "label": "A",
    "plan_type": "FREE",
    "enabled": 1,
    "status": "ACTIVE",
    "priority": 0,
    "quota_group": "account-a",
    "trial_expires_at": None,
}
TARGET = {
    "position": 1,
    "provider_id": "custom",
    "model": "model",
    "credential_id": None,
    "input_price": None,
    "output_price": None,
}


@pytest.mark.parametrize(
    ("provider", "status", "error", "kind", "scope", "state", "retry"),
    [
        ("openai", 401, {}, "auth", "credential", "INVALID", True),
        ("gemini", 400, {"message": "API key not valid"}, "auth", "credential", "INVALID", True),
        ("gemini", 403, {"message": "Key reported leaked"}, "auth", "credential", "INVALID", True),
        (
            "groq",
            403,
            {"message": "Model access denied"},
            "model_access",
            "credential_model",
            "DEGRADED",
            True,
        ),
        ("nim", 404, {"message": "Model not found"}, "model_access", "target", "DEGRADED", True),
        (
            "openai",
            429,
            {"code": "insufficient_quota"},
            "funds_or_account",
            "credential",
            "UNUSABLE",
            True,
        ),
        ("custom", 402, {}, "funds_or_account", "credential", "UNUSABLE", True),
        (
            "custom",
            403,
            {"message": "Account suspended"},
            "funds_or_account",
            "credential",
            "UNUSABLE",
            True,
        ),
        (
            "openai",
            400,
            {"code": "context_length_exceeded"},
            "context_length",
            "next_target",
            None,
            True,
        ),
        ("custom", 413, {}, "context_length", "next_target", None, True),
        ("custom", 400, {"message": "Bad request"}, "request_error", None, None, False),
        ("custom", 422, {}, "request_error", None, None, False),
        ("groq", 503, {}, "provider_failure", "target", "DEGRADED", True),
        ("custom", 200, None, "ok", None, None, False),
    ],
)
def test_classifier_matrix(provider, status, error, kind, scope, state, retry):
    body = json.dumps({"error": error} if error is not None else {"choices": []}).encode()
    result = classify(provider, status, {}, body, NOW)
    assert (result.kind, result.scope, result.state, result.retry) == (kind, scope, state, retry)


def test_unknown_429_ladder_success_reset_and_explicit_reset():
    outcome = classify("custom", 429, {}, b"{}", NOW)
    current = {}
    for seconds in (5, 15, 60, 300, 1800, 7200, 7200):
        current = next_quota_state(current, outcome, NOW)
        assert current["status"] == "COOLDOWN"
        assert datetime.fromisoformat(current["until"]) == NOW + timedelta(seconds=seconds)
    current = next_quota_state(current, classify("custom", 200, {}, b"{}", NOW), NOW)
    assert current["strikes"] == 0
    result = classify("custom", 429, {"retry-after": "120"}, b"{}", NOW)
    assert datetime.fromisoformat(result.until) == NOW + timedelta(seconds=120)
    assert retry_after({"retry-after": "Tue, 06 Oct 2026 12:01:00 GMT"}, NOW) == NOW + timedelta(
        minutes=1
    )
    for value in ("NaN", "infinity", "invalid"):
        assert retry_after({"retry-after": value}, NOW) is None


def test_daily_reset_is_evidence_based_and_pacific_dst_aware():
    body = b'{"error":{"message":"Quota exceeded: requests per day"}}'
    result = classify("gemini", 429, {}, body, NOW)
    assert result.state == "EXHAUSTED"
    assert result.until == "2026-10-07T07:00:00+00:00"
    assert classify("custom", 429, {}, body, NOW).state == "COOLDOWN"


@pytest.mark.parametrize(
    ("change", "policy", "usage", "reason"),
    [
        ({"enabled": 0}, {}, {}, "disabled"),
        ({"status": "INVALID"}, {}, {}, "invalid"),
        ({"trial_expires_at": NOW.isoformat()}, {}, {}, "expired"),
        ({"plan_type": "TRIAL_CREDIT"}, {"allow_trial": 0}, {}, "trial_blocked"),
        ({"plan_type": "PAID"}, {}, {}, "paid_blocked"),
        ({"plan_type": "UNKNOWN"}, {}, {}, "paid_blocked"),
        ({"secret_available": False}, {}, {}, "missing_secret"),
        (
            {"plan_type": "PAID"},
            {"allow_paid": 1, "paid_daily_cap_usd": 1},
            {"daily": 1},
            "cap_reached",
        ),
        (
            {"plan_type": "PAID"},
            {"allow_paid": 1, "paid_monthly_cap_usd": 1},
            {"monthly": 1},
            "cap_reached",
        ),
        ({"plan_type": "PAID"}, {"allow_paid": 1, "paid_daily_cap_usd": 1}, {}, "unknown_price"),
        (
            {"plan_type": "PAID"},
            {"allow_paid": 1, "paid_daily_cap_usd": 1},
            {"unknown": True},
            "unknown_spend",
        ),
    ],
)
def test_skip_reasons(change, policy, usage, reason):
    result = candidates(PROFILE | policy, [TARGET], [KEY | change], {}, usage, NOW)
    assert result[0]["skip_reason"] == reason


def test_order_pool_quota_group_model_scope_and_override():
    credentials = [
        KEY | {"id": 2, "priority": -1},
        KEY,
        KEY | {"id": 3, "quota_group": "account-b"},
    ]
    states = {
        ("custom:account-a", "model"): {
            "status": "COOLDOWN",
            "until": (NOW + timedelta(seconds=5)).isoformat(),
        }
    }
    decisions = candidates(
        PROFILE, [TARGET, TARGET | {"position": 2, "model": "other"}], credentials, states, {}, NOW
    )
    assert [d["credential_id"] for d in decisions] == [2, 1, 3, 2, 1, 3]
    assert [d["eligible"] for d in decisions] == [False, False, True, True, True, True]
    paid = KEY | {"plan_type": "PAID"}
    policy = PROFILE | {"allow_paid": 1, "paid_daily_cap_usd": 1, "allow_unknown_price": 1}
    assert candidates(policy, [TARGET], [paid], {}, {}, NOW)[0]["eligible"]
    assert candidates(PROFILE, [TARGET], [KEY], states, {}, NOW + timedelta(seconds=6))[0][
        "eligible"
    ]


def test_cost_truth_and_invalid_usage():
    decision = {"input_price": 2, "output_price": 4}
    result = usage_metadata(
        b'{"usage":{"prompt_tokens":10,"completion_tokens":20,"cost_usd":0.5}}', decision
    )
    assert result["provider_cost_usd"] == 0.5
    assert result["estimated_cost_usd"] == 0.0001
    invalid = usage_metadata(
        b'{"usage":{"prompt_tokens":-1,"completion_tokens":true,"cost":"400 credits"}}', decision
    )
    assert all(value is None for value in invalid.values())


def test_documented_headers_and_cost_units():
    outcome = classify(
        "openai",
        429,
        {
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "1s",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "6m0s",
        },
        b"{}",
        NOW,
    )
    assert outcome.until == (NOW + timedelta(minutes=6)).isoformat()
    assert (
        usage_metadata(b'{"usage":{"cost":400}}', {"provider_id": "custom"})["provider_cost_usd"]
        is None
    )
    assert (
        usage_metadata(b'{"usage":{"cost":0.2}}', {"provider_id": "openrouter"})[
            "provider_cost_usd"
        ]
        == 0.2
    )


def test_daily_unknown_spend_does_not_leak_into_next_day_only_cap():
    profile = PROFILE | {"allow_paid": 1, "paid_daily_cap_usd": 1}
    target = TARGET | {"input_price": 1, "output_price": 1}
    usage = {"unknown": True, "unknown_daily": False, "unknown_monthly": True}
    assert candidates(profile, [target], [KEY | {"plan_type": "PAID"}], {}, usage, NOW)[0][
        "eligible"
    ]
    profile["paid_monthly_cap_usd"] = 10
    assert (
        candidates(profile, [target], [KEY | {"plan_type": "PAID"}], {}, usage, NOW)[0][
            "skip_reason"
        ]
        == "unknown_spend"
    )


def test_empty_pool_is_explained_and_disabled_route_is_blocked():
    decisions = candidates(PROFILE, [TARGET], [], {}, {}, NOW)
    assert decisions[0]["skip_reason"] == "no_credentials"
    assert (
        candidates(PROFILE | {"enabled": 0}, [TARGET], [KEY], {}, {}, NOW)[0]["skip_reason"]
        == "disabled"
    )
