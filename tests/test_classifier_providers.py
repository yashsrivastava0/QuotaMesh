"""Real (live-captured) and synthetic provider error shapes map to the right recovery scope."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quotamesh.engine.classify import (
    classify,
    error_status,
    extract_error,
    parse_duration,
    usage_metadata,
)
from quotamesh.observations import rate_observations

FIXTURES = Path(__file__).parent / "fixtures" / "providers"
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)  # 05:00 in Los Angeles (PDT)
PACIFIC_MIDNIGHT = "2026-10-17T07:00:00+00:00"

# Live captures from docs/live-provider-notes.md, keyed by provider/case:
# (kind, scope, state, retry). A new error fixture without an entry fails loudly.
EXPECTED = {
    "gemini/invalid_key_bearer": ("auth", "credential", "INVALID", True),
    "gemini/invalid_key_x_goog_api_key": ("auth", "credential", "INVALID", True),
    "gemini/unknown_model": ("model_access", "target", "DEGRADED", True),
    "gemini/json_mode": ("provider_failure", "target", "DEGRADED", True),
    "gemini/models_auth_x_goog_api_key": ("model_access", "target", "DEGRADED", True),
    "openai/invalid_key": ("auth", "credential", "INVALID", True),
    "openai/unknown_model": ("model_access", "target", "DEGRADED", True),
    "openai/gpt5_max_tokens": ("unsupported_parameter", None, None, False),
}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def live_error_fixtures():
    cases = []
    for provider in ("openai", "gemini"):
        for path in sorted((FIXTURES / provider).glob("*.json")):
            data = load(path)
            if isinstance(data.get("status"), int) and data["status"] >= 400 and "body" in data:
                cases.append((f"{provider}/{path.stem}", data))
    return cases


def until(spec):
    if spec is None:
        return None
    if spec == "midnight_pt":
        return PACIFIC_MIDNIGHT
    assert spec.startswith("+") and spec.endswith("s"), spec
    return (NOW + timedelta(seconds=float(spec[1:-1]))).isoformat()


@pytest.mark.parametrize(
    ("key", "data"), [pytest.param(key, data, id=key) for key, data in live_error_fixtures()]
)
def test_live_provider_errors_classify_by_evidence(key, data):
    body = json.dumps(data["body"]).encode()  # Gemini sends a root array; OpenAI an object
    result = classify(data["provider"], data["status"], data["headers"], body, NOW)
    assert (result.kind, result.scope, result.state, result.retry) == EXPECTED[key]


def test_expectation_table_matches_the_captured_fixtures():
    assert {key for key, _ in live_error_fixtures()} == set(EXPECTED)


@pytest.mark.parametrize(
    "path", sorted((FIXTURES / "synthetic").glob("*.json")), ids=lambda path: path.stem
)
def test_synthetic_provider_errors(path):
    data = load(path)
    assert data["source"] == "synthetic" or data["source"].startswith("docs https://")
    status = data["status"]
    if data.get("sse_event"):
        # A first SSE event arrives on HTTP 200; the gateway derives the status from its error.
        status = error_status(data["body"]["error"])
    body = json.dumps(data["body"]).encode()
    result = classify(data["provider"], status, data["headers"], body, NOW)
    expect = data["expect"]
    assert (result.kind, result.scope, result.state) == (
        expect["kind"],
        expect["scope"],
        expect["state"],
    )
    assert result.until == until(expect["until"])
    assert result.retry


def test_live_gemini_invalid_key_is_no_longer_a_terminal_request_error():
    data = load(FIXTURES / "gemini" / "invalid_key_bearer.json")
    facts = extract_error(json.dumps(data["body"]))
    assert (
        facts["error"] and facts["numeric"] == 400 and facts["google_status"] == "INVALID_ARGUMENT"
    )
    assert "pass a valid api key" in facts["text"]


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("39s", 39.0), ("1.5s", 1.5), ("0s", 0.0), ("604800s", 604800.0)],
)
def test_parse_duration_accepts_protobuf_seconds(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize(
    "value", ["", "39", "s", "-1s", "1e3s", "1.s", "604801s", "39 s", "٣٩s", "39s\n", None, 39]
)
def test_parse_duration_rejects_everything_else(value):
    assert parse_duration(value) is None


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ({"code": 429, "status": "RESOURCE_EXHAUSTED"}, 429),
        ({"code": 200, "status": 401}, 401),
        ({"code": "429", "status": "RESOURCE_EXHAUSTED"}, 429),
        ({"status": "resource_exhausted"}, 429),
        ({"status": "UNAUTHENTICATED"}, 401),
        ({"status": "PERMISSION_DENIED"}, 403),
        ({"status": "NOT_FOUND"}, 404),
        ({"status": "INVALID_ARGUMENT"}, 400),
        ({"status": "FAILED_PRECONDITION"}, 400),
        ({"status": "UNAVAILABLE"}, 503),
        ({"status": "INTERNAL"}, 500),
        ({"status": "DEADLINE_EXCEEDED"}, 504),
        ({"status": "SOMETHING_NEW"}, 502),
        ({"code": True}, 502),
        ({"code": 700}, 502),
        ({}, 502),
        ("rate limited", 502),
        (None, 502),
    ],
)
def test_error_status_prefers_numbers_then_google_names(error, status):
    assert error_status(error) == status


def test_extract_error_shapes_and_bounds():
    listed = extract_error(b'[{"note": "x"}, {"error": {"message": "second", "code": "Bad_Code"}}]')
    assert listed["error"] and listed["code"] == "bad_code" and "second" in listed["text"]
    assert extract_error(b'[{"message": "first dict"}]')["text"] == "first dict"
    assert extract_error(b'{"error": "Invalid API key"}')["text"] == "invalid api key"
    detail = extract_error(b'{"detail": "Not Found"}')
    assert detail["text"] == "not found" and not detail["error"]
    for broken in (b"", b"not json", b"[]", b"[1, 2]", b"null", b"\xed\xb3\xbf"):
        assert not extract_error(broken)["error"] and extract_error(broken)["text"] == ""
    linked = extract_error(
        b'{"error": {"message": "Visit https://platform.openai.com/account/billing now"}}'
    )
    assert "billing" not in linked["text"] and "visit" in linked["text"]
    details = [
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": f"REASON_{i}"}
        for i in range(25)
    ]
    long_text = extract_error(
        json.dumps({"error": {"message": "x" * 5000, "status": "y" * 5000, "details": details}})
    )
    assert long_text["reasons"] == [f"REASON_{i}" for i in range(20)]
    assert len(long_text["google_status"]) == 2048
    assert long_text["text"].count("x") == 2048
    quota = extract_error(
        json.dumps(
            {
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [{"quotaId": f"Q{i}"} for i in range(30)] + ["x"],
                        },
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "2s"},
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "9s"},
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "x"},
                        "not a detail",
                    ]
                }
            }
        )
    )
    assert quota["quota_ids"] == [f"Q{i}" for i in range(20)] and quota["retry_delay_s"] == 9


def test_reason_only_api_key_invalid_is_auth():
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": "",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                    }
                ],
            }
        }
    )
    assert classify("custom", 400, {}, body, NOW).state == "INVALID"


@pytest.mark.parametrize(
    ("status", "message", "kind", "state"),
    [
        # Weak billing wording without quota evidence is still an account problem...
        (400, "Billing is not set up for this account", "funds_or_account", "UNUSABLE"),
        (400, "Insufficient balance: top up to continue", "funds_or_account", "UNUSABLE"),
        # ...but a billing *link* is not billing evidence, and a 429 is always a throttle.
        (403, "Denied. See https://example.com/billing", "model_access", "DEGRADED"),
        (429, "Check your plan and billing details", "rate_limit", "COOLDOWN"),
        (402, "Payment required", "funds_or_account", "UNUSABLE"),
    ],
)
def test_weak_billing_wording_only_after_quota_evidence(status, message, kind, state):
    body = json.dumps({"error": {"message": message}})
    result = classify("custom", status, {}, body, NOW)
    assert (result.kind, result.state) == (kind, state)


def test_success_status_with_error_body_and_top_level_messages():
    assert classify("custom", 200, {}, b'{"error": {"code": 429}}', NOW).kind == "upstream_error"
    assert classify("custom", 200, {}, b'{"message": "rate limit"}', NOW).kind == "ok"
    assert classify("custom", 200, {}, b'{"error": null, "choices": []}', NOW).kind == "ok"
    assert classify("custom", 401, {}, b"<html>denied</html>", NOW).kind == "auth"
    assert classify("custom", 400, {}, b'{"detail": "API key not valid"}', NOW).kind == "auth"
    assert classify("custom", 400, {}, b'{"message": "Unsupported field"}', NOW).kind == (
        "request_error"
    )


def test_daily_reset_rule_comes_from_the_registry():
    daily = json.dumps(
        {
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"},
                ],
            }
        }
    )
    # Gemini (reset_rule = midnight_pt) waits for Pacific midnight.
    assert classify("gemini", 429, {}, daily, NOW).until == PACIFIC_MIDNIGHT
    # A longer explicit reset still wins over midnight.
    later = classify("gemini", 429, {"Retry-After": "86400"}, daily, NOW)
    assert later.until == (NOW + timedelta(days=1)).isoformat()
    # Without a registry rule, daily evidence needs an explicit reset to become EXHAUSTED.
    custom = classify("custom", 429, {}, daily, NOW)
    assert (custom.state, custom.until) == ("EXHAUSTED", (NOW + timedelta(seconds=21)).isoformat())
    unknown = classify("not-in-registry", 429, {}, b'{"error":{"message":"daily limit"}}', NOW)
    assert (unknown.state, unknown.until) == ("COOLDOWN", None)


def test_cooldown_uses_the_longest_explicit_reset():
    body = json.dumps(
        {
            "error": {
                "code": 429,
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "39s"}
                ],
            }
        }
    )
    result = classify("gemini", 429, {"Retry-After": "5"}, body, NOW)
    assert (result.state, result.until) == ("COOLDOWN", (NOW + timedelta(seconds=39)).isoformat())
    result = classify("gemini", 429, {"Retry-After": "120"}, body, NOW)
    assert result.until == (NOW + timedelta(seconds=120)).isoformat()


def test_thinking_tokens_are_reported_as_output():
    decision = {"input_price": 1, "output_price": 2}

    def usage(**values):
        return usage_metadata(json.dumps({"usage": values}), decision)

    thinking = usage(prompt_tokens=8, completion_tokens=1, total_tokens=40)
    assert (thinking["input_tokens"], thinking["output_tokens"]) == (8, 32)
    assert thinking["estimated_cost_usd"] == (8 * 1 + 32 * 2) / 1e6
    assert usage(prompt_tokens=8, completion_tokens=1, total_tokens=9)["output_tokens"] == 1
    assert usage(prompt_tokens=8, completion_tokens=5, total_tokens=9)["output_tokens"] == 5
    for total in (True, -1, "40", 2**64, None):
        assert usage(prompt_tokens=8, completion_tokens=1, total_tokens=total)["output_tokens"] == 1
    assert usage(prompt_tokens=8, total_tokens=40)["output_tokens"] is None


def test_live_openai_rate_headers_are_parsed_passively():
    headers = load(FIXTURES / "openai" / "gpt5_max_tokens.json")["headers"]
    rows = {row["dimension"]: row for row in rate_observations("openai", headers, NOW)}
    assert rows["requests"]["remaining"] == 4999
    assert rows["requests"]["reset_at"] == (NOW + timedelta(milliseconds=12)).isoformat()
    assert rows["tokens"]["reset_at"] == NOW.isoformat()
