"""Body-aware classification. Messages are inspected transiently, never saved."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from quotamesh.config import registry

MAX_DETAILS = 20  # bounded inspection of Google error details and quota violations
MAX_TEXT = 2048  # every inspected string is truncated to 2 KB
URL = re.compile(r"https?://\S+")
GOOGLE_STATUS = {
    "RESOURCE_EXHAUSTED": 429,
    "UNAUTHENTICATED": 401,
    "PERMISSION_DENIED": 403,
    "NOT_FOUND": 404,
    "INVALID_ARGUMENT": 400,
    "FAILED_PRECONDITION": 400,
    "UNAVAILABLE": 503,
    "INTERNAL": 500,
    "DEADLINE_EXCEEDED": 504,
}
AUTH_PHRASES = (
    "access_token_type_unsupported",
    "api key not valid",
    "pass a valid api key",
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "api_key_invalid",
    "api key expired",
    "expired api key",
    # Gemini's OpenAI-compatible endpoint: "Missing or invalid Authorization header."
    "invalid authorization header",
)
FUNDS_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "billing_not_active",
        "billing_hard_limit_reached",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)
REGION_PHRASES = (
    "country, region, or territory not supported",
    "user location is not supported",
    "not available in your country",
    "unsupported country",
    "region blocked",
)
ACCOUNT_REASONS = frozenset({"SERVICE_DISABLED", "API_KEY_SERVICE_BLOCKED", "CONSUMER_SUSPENDED"})
ACCOUNT_PHRASES = ("account suspended", "account deactivated")
DAILY_PHRASES = ("per day", "daily", "requestsperday", "rpd")
WEAK_FUNDS_PHRASES = ("payment required", "billing", "insufficient balance", "credit balance")
CONTEXT_PHRASES = (
    "context length",
    "context_length",
    "maximum context",
    "exceeds the maximum number of tokens",
    "input token count",
)


@dataclass(frozen=True)
class Outcome:
    kind: str
    retry: bool
    scope: str | None = None
    state: str | None = None
    until: str | None = None


def retry_after(headers, now):
    value = headers.get("retry-after", "")
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            return None
        return now + timedelta(seconds=max(0, min(seconds, 604800)))
    except (ValueError, OverflowError):
        try:
            result = parsedate_to_datetime(value)
            return max(now, result.astimezone(UTC)) if result.tzinfo else None
        except (ValueError, TypeError, OverflowError):
            return None


def documented_reset(provider, headers, now):
    if provider not in {"openai", "groq"}:
        return None
    resets = []
    for suffix in ("requests", "tokens", "project-tokens"):
        if headers.get("x-ratelimit-remaining-" + suffix) != "0":
            continue
        value = headers.get("x-ratelimit-reset-" + suffix, "")
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
        if not parts or "".join(number + unit for number, unit in parts) != value:
            continue
        seconds = sum(
            float(number) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
            for number, unit in parts
        )
        if math.isfinite(seconds) and 0 <= seconds <= 604800:
            resets.append(now + timedelta(seconds=seconds))
    return max(resets) if resets else None


def parse_duration(value):
    """Parse a protobuf duration such as Google's RetryInfo "39s" or "1.5s"."""
    if not isinstance(value, str) or len(value) > 32:
        return None
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", value):
        return None
    seconds = float(value[:-1])
    return seconds if math.isfinite(seconds) and 0 <= seconds <= 604800 else None


def _text(value):
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def error_status(error):
    """Derive an HTTP-like status from an error object, such as a first SSE event's."""
    if not isinstance(error, dict):
        return 502
    for field in ("code", "status"):
        value = _integer(error.get(field))
        if value is not None and 400 <= value <= 599:
            return value
    status = error.get("status")
    return GOOGLE_STATUS.get(status.upper(), 502) if isinstance(status, str) else 502


def extract_error(body):
    """Return bounded, transient facts about an error body; nothing here is ever stored.

    Accepts OpenAI-style objects, Google root arrays (``[{"error": {...}}]``), string errors,
    and top-level ``message``/``detail`` strings. ``error`` is True only when the body carried
    a non-empty ``error`` member. ``text`` is the lowercased message without URLs, plus the
    code, type, Google status, ErrorInfo reasons and quota ids.
    """
    facts = {
        "error": False,
        "code": "",
        "numeric": None,
        "type": "",
        "google_status": "",
        "reasons": [],
        "quota_ids": [],
        "retry_delay_s": None,
        "text": "",
    }
    try:
        root = json.loads(body)
    except (ValueError, TypeError, UnicodeDecodeError):
        return facts
    if isinstance(root, list):
        dicts = [item for item in root[:MAX_DETAILS] if isinstance(item, dict)]
        root = next((item for item in dicts if "error" in item), dicts[0] if dicts else None)
    if not isinstance(root, dict):
        return facts
    error = root.get("error")
    facts["error"] = bool(error)
    if isinstance(error, dict):
        code = error.get("code")
        facts["code"] = _text(code).lower()
        numeric = _integer(code)
        facts["numeric"] = numeric if numeric is not None else _integer(error.get("status"))
        facts["type"] = _text(error.get("type")).lower()
        facts["google_status"] = _text(error.get("status")).upper()
        message = _text(error.get("message"))
        details = error.get("details")
        for item in details[:MAX_DETAILS] if isinstance(details, list) else ():
            kind = _text(item.get("@type")) if isinstance(item, dict) else ""
            if kind.endswith("ErrorInfo"):
                reason = _text(item.get("reason")).upper()
                if reason:
                    facts["reasons"].append(reason)
            elif kind.endswith("QuotaFailure"):
                violations = item.get("violations")
                for violation in violations[:MAX_DETAILS] if isinstance(violations, list) else ():
                    quota_id = (
                        _text(violation.get("quotaId")) if isinstance(violation, dict) else ""
                    )
                    if quota_id:
                        facts["quota_ids"].append(quota_id)
            elif kind.endswith("RetryInfo"):
                delay = parse_duration(item.get("retryDelay"))
                if delay is not None:
                    facts["retry_delay_s"] = max(delay, facts["retry_delay_s"] or 0)
    elif isinstance(error, str) and error:
        message = _text(error)
    else:
        # No usable "error" member: only a failing status makes these strings evidence.
        message = _text(root.get("message")) or _text(root.get("detail"))
    parts = [
        URL.sub(" ", message).lower(),
        facts["code"],
        facts["type"],
        facts["google_status"].lower(),
        *(reason.lower() for reason in facts["reasons"]),
        *(quota_id.lower() for quota_id in facts["quota_ids"]),
    ]
    facts["text"] = " ".join(part for part in parts if part)
    return facts


def _reset_rule(provider):
    entry = registry().get(provider) if isinstance(provider, str) else None
    return entry.reset_rule if entry else ""


def _next_pacific_midnight(now):
    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    return (
        local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    ).astimezone(UTC)


def _quota_outcome(provider, headers, facts, now):
    delay = facts["retry_delay_s"]
    explicit = [
        reset
        for reset in (
            retry_after(headers, now),
            now + timedelta(seconds=delay) if delay is not None else None,
            documented_reset(provider, headers, now),
        )
        if reset is not None
    ]
    daily = any("perday" in quota_id.lower() for quota_id in facts["quota_ids"]) or any(
        phrase in facts["text"] for phrase in DAILY_PHRASES
    )
    if daily and _reset_rule(provider) == "midnight_pt":
        # A short retry hint never shortens a daily block; a longer explicit reset wins.
        until = max([_next_pacific_midnight(now), *explicit])
        return Outcome("daily_quota", True, "quota", "EXHAUSTED", until.isoformat())
    reset = max(explicit, default=None)
    if daily and reset:
        return Outcome("daily_quota", True, "quota", "EXHAUSTED", reset.isoformat())
    return Outcome("rate_limit", True, "quota", "COOLDOWN", reset.isoformat() if reset else None)


def classify(provider, status, headers, body, now):
    """Classify one upstream result. Rules are ordered and the first match wins:

    auth, structured funds, region, account/service, quota/rate, weak billing wording,
    context length, model access, provider failure, then terminal request errors.
    """
    headers = {key.lower(): value for key, value in headers.items()}
    facts = extract_error(body)
    if status < 400:
        if facts["error"]:
            return Outcome("upstream_error", True, "target", "DEGRADED")
        return Outcome("ok", False)
    code, kind, google, text = facts["code"], facts["type"], facts["google_status"], facts["text"]
    reasons = set(facts["reasons"])
    if (
        status == 401
        or google == "UNAUTHENTICATED"
        or code == "invalid_api_key"
        or "API_KEY_INVALID" in reasons
        or any(phrase in text for phrase in AUTH_PHRASES)
        or ("key" in text and any(word in text for word in ("revoked", "leaked", "expired")))
    ):
        return Outcome("auth", True, "credential", "INVALID")
    # OpenAI reports insufficient_quota with HTTP 429, so funds precede rate limits.
    if status == 402 or code in FUNDS_CODES or kind in FUNDS_CODES:
        return Outcome("funds_or_account", True, "credential", "UNUSABLE")
    if code == "unsupported_country_region_territory" or any(
        phrase in text for phrase in REGION_PHRASES
    ):
        return Outcome("region_blocked", True, "credential", "UNUSABLE")
    if reasons & ACCOUNT_REASONS or any(phrase in text for phrase in ACCOUNT_PHRASES):
        return Outcome("funds_or_account", True, "credential", "UNUSABLE")
    if (
        status == 429
        or google == "RESOURCE_EXHAUSTED"
        or code in {"rate_limit_exceeded", "slow_down"}
        or kind == "rate_limit_error"
    ):
        return _quota_outcome(provider, headers, facts, now)
    # Weak billing wording only after quota evidence: "check your plan and billing details"
    # on a 429 is a throttle, never a sticky unusable key.
    if any(phrase in text for phrase in WEAK_FUNDS_PHRASES):
        return Outcome("funds_or_account", True, "credential", "UNUSABLE")
    if (
        status == 413
        or code == "context_length_exceeded"
        or any(phrase in text for phrase in CONTEXT_PHRASES)
    ):
        return Outcome("context_length", True, "next_target")
    if status == 404 or code == "model_not_found" or google == "NOT_FOUND":
        return Outcome("model_access", True, "target", "DEGRADED")
    if status == 403 or google == "PERMISSION_DENIED" or "organization must be verified" in text:
        return Outcome("model_access", True, "credential_model", "DEGRADED")
    if (
        status >= 500
        or google in {"UNAVAILABLE", "INTERNAL"}
        or code in {"server_is_overloaded", "server_error"}
        or "overloaded" in text
    ):
        return Outcome("provider_failure", True, "target", "DEGRADED")
    if code in {"unsupported_parameter", "unsupported_value"}:
        return Outcome("unsupported_parameter", False)
    return Outcome("request_error", False)


def next_quota_state(current, outcome, now):
    if outcome.kind == "ok":
        return {"status": "OK", "until": None, "strikes": 0, "reason": None}
    strikes = current.get("strikes", 0) + 1
    ladder = (5, 15, 60, 300, 1800, 7200)
    until = outcome.until or (now + timedelta(seconds=ladder[min(strikes - 1, 5)])).isoformat()
    return {"status": outcome.state, "until": until, "strikes": strikes, "reason": outcome.kind}


def sse_payload(event):
    data = b"\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:"))
    if not data:
        return None
    if data == b"[DONE]":
        raise ValueError("Stream ended without a response event")
    obj = json.loads(data)
    if not isinstance(obj, dict) or not ("choices" in obj or "error" in obj):
        raise ValueError("Invalid Chat Completions event")
    return obj


def valid_price(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _token_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1


def usage_metadata(body, decision):
    try:
        obj = json.loads(body)
        usage = obj.get("usage") or {}
        tokens = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        tokens = [n if _token_count(n) else None for n in tokens]
        total = usage.get("total_tokens")
        if None not in tokens and _token_count(total) and total > tokens[0] + tokens[1]:
            # Hidden reasoning ("thinking") tokens are billed as output (live notes D7).
            tokens[1] = total - tokens[0]
        cost = usage.get("cost_usd")
        if cost is None and decision.get("provider_id") == "openrouter":
            cost = usage.get("cost")  # This extension is documented in USD for OpenRouter only.
        # Bound a single-call USD observation to the same range as manual money fields.
        # Implausible values stay unknown instead of overflowing durable sums.
        provider_cost = cost if valid_price(cost) and cost <= 1_000_000 else None
        estimated = None
        if all(n is not None for n in tokens) and all(
            decision.get(p) is not None for p in ("input_price", "output_price")
        ):
            estimated = (
                tokens[0] * decision["input_price"] + tokens[1] * decision["output_price"]
            ) / 1e6
        return {
            "input_tokens": tokens[0],
            "output_tokens": tokens[1],
            "provider_cost_usd": provider_cost,
            "estimated_cost_usd": estimated,
        }
    except (ValueError, AttributeError, TypeError):
        return {}
