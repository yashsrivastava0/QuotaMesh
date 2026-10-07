"""Allowlisted passive rate evidence; never stores raw headers or makes probes."""

import re
from datetime import timedelta


def rate_observations(provider, headers, now):
    if provider not in {"openai", "groq"}:
        return []
    rows = []
    for dimension in ("requests", "tokens", "project-tokens"):
        if provider == "groq" and dimension == "project-tokens":
            continue
        value = headers.get("x-ratelimit-remaining-" + dimension, "")
        if not re.fullmatch(r"[0-9]{1,18}", value):
            continue
        reset = headers.get("x-ratelimit-reset-" + dimension, "")
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", reset)
        seconds = None
        if parts and "".join(n + unit for n, unit in parts) == reset and len(reset) < 100:
            total = sum(float(n) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[u] for n, u in parts)
            if 0 <= total <= 604800:
                seconds = total
        rows.append(
            {
                "dimension": dimension,
                "remaining": int(value),
                "window": ("day" if dimension == "requests" else "minute")
                if provider == "groq"
                else "provider window",
                "observed_at": now.isoformat(),
                "reset_at": (now + timedelta(seconds=seconds)).isoformat()
                if seconds is not None
                else None,
                # Even before reset, other traffic may have consumed this observation.
                "stale_at": (
                    now + timedelta(seconds=min(seconds, 60) if seconds is not None else 60)
                ).isoformat(),
            }
        )
    return rows
