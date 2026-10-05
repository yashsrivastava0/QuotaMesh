"""Body-aware classification. Messages are inspected transiently, never saved."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo


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


def classify(provider, status, headers, body, now):
    headers = {key.lower(): value for key, value in headers.items()}
    try:
        obj = json.loads(body)
        error = obj.get("error") if isinstance(obj, dict) else None
    except (ValueError, UnicodeDecodeError):
        error = None
    text = json.dumps(error).lower() if error else ""
    if (
        status == 401
        or any(
            word in text
            for word in (
                "api_key_invalid",
                "api_key_expired",
                "api key expired",
                "invalid api key",
                "api key not valid",
                "expired api key",
                "invalid_api_key",
            )
        )
        or ("key" in text and any(word in text for word in ("revoked", "leaked", "expired")))
    ):
        return Outcome("auth", True, "credential", "INVALID")
    if status == 402 or any(
        word in text
        for word in (
            "insufficient_quota",
            "payment required",
            "billing",
            "account suspended",
            "unsupported country",
            "region blocked",
        )
    ):
        return Outcome("funds_or_account", True, "credential", "UNUSABLE")
    if status == 429 or any(word in text for word in ("resource_exhausted", "rate_limit_exceeded")):
        reset = retry_after(headers, now) or documented_reset(provider, headers, now)
        daily = any(word in text for word in ("per day", "daily", "requestsperday", "rpd"))
        if daily and reset is None and provider == "gemini":
            local = now.astimezone(ZoneInfo("America/Los_Angeles"))
            reset = (
                local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            ).astimezone(UTC)
        return Outcome(
            "daily_quota" if daily and reset else "rate_limit",
            True,
            "quota",
            "EXHAUSTED" if daily and reset else "COOLDOWN",
            reset.isoformat() if reset else None,
        )
    if status == 413 or "context_length" in text or "context length" in text:
        return Outcome("context_length", True, "next_target")
    if status == 403:
        return Outcome("model_access", True, "credential_model", "DEGRADED")
    if status == 404:
        return Outcome("model_access", True, "target", "DEGRADED")
    if status >= 500 or any(word in text for word in ("overloaded", "server_error")):
        return Outcome("provider_failure", True, "target", "DEGRADED")
    if status >= 400:
        return Outcome("request_error", False)
    if error:
        return Outcome("upstream_error", True, "target", "DEGRADED")
    return Outcome("ok", False)


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


def usage_metadata(body, decision):
    try:
        obj = json.loads(body)
        usage = obj.get("usage") or {}
        tokens = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        tokens = [
            n if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 2**63 - 1 else None
            for n in tokens
        ]
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
