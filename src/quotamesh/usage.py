"""Durable observation summaries. Missing provider evidence never becomes a measured zero."""

from __future__ import annotations


def summarize(rows):
    result = {
        "attempts": 0,
        "routed_requests": 0,
        "failures": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "input_missing": 0,
        "output_missing": 0,
        "provider_cost_usd": 0,
        "estimated_cost_usd": 0,
        "unknown_cost": 0,
        "provider_cost_count": 0,
        "estimated_cost_count": 0,
        "last_success_at": None,
        "last_error_at": None,
        "last_error_class": None,
        "last_seen_at": None,
        "last_latency_ms": None,
    }
    for row in rows:
        for name in (
            "attempts",
            "routed_requests",
            "failures",
            "input_tokens",
            "output_tokens",
            "input_missing",
            "output_missing",
            "provider_cost_usd",
            "estimated_cost_usd",
            "unknown_cost",
            "provider_cost_count",
            "estimated_cost_count",
        ):
            result[name] += row[name]
        for key in ("last_success_at", "last_error_at"):
            if row[key] and (result[key] is None or row[key] > result[key]):
                result[key] = row[key]
                if key == "last_error_at":
                    result["last_error_class"] = row["last_error_class"]
        if result["last_seen_at"] is None or row["last_seen_at"] > result["last_seen_at"]:
            result["last_seen_at"] = row["last_seen_at"]
            result["last_latency_ms"] = row["last_latency_ms"]
    # A completely missing quantity remains null; partial observations are explicitly incomplete.
    for name in ("input_tokens", "output_tokens"):
        missing = result[name.split("_")[0] + "_missing"]
        if not result["attempts"] or (missing and not result[name]):
            result[name] = None
    result["known_cost_usd"] = result["provider_cost_usd"] + result["estimated_cost_usd"]
    result["sources"] = {
        "attempts": "LOCAL",
        "routed_requests": "LOCAL",
        "tokens": "PROVIDER"
        if result["input_tokens"] is not None or result["output_tokens"] is not None
        else "UNKNOWN",
        "provider_cost_usd": "PROVIDER" if result["provider_cost_count"] else "UNKNOWN",
        "estimated_cost_usd": "LOCAL" if result["estimated_cost_count"] else "UNKNOWN",
        "unknown_cost": "UNKNOWN",
        "timestamps": "LOCAL",
    }
    return result


def record_rollup(conn, values):
    cost = values.get("provider_cost_usd")
    estimated = values.get("estimated_cost_usd") if cost is None else None
    now = values["ts"]
    ok = values["outcome"] == "OK"
    columns = [
        "day",
        "profile_id",
        "credential_id",
        "provider_id",
        "model",
        "plan_type",
        "attempts",
        "routed_requests",
        "failures",
        "input_tokens",
        "output_tokens",
        "input_missing",
        "output_missing",
        "provider_cost_usd",
        "estimated_cost_usd",
        "unknown_cost",
        "provider_cost_count",
        "estimated_cost_count",
        "last_success_at",
        "last_error_at",
        "last_error_class",
        "last_seen_at",
        "last_latency_ms",
    ]
    data = [
        now[:10],
        values["profile_id"],
        values.get("credential_id") or 0,
        values["provider_id"],
        values["model"],
        values.get("plan_type") or "UNKNOWN",
        1,
        int(values["attempt_idx"] == 1),
        int(not ok),
        values.get("input_tokens") or 0,
        values.get("output_tokens") or 0,
        int(values.get("input_tokens") is None),
        int(values.get("output_tokens") is None),
        cost or 0,
        estimated or 0,
        int(cost is None and estimated is None),
        int(cost is not None),
        int(estimated is not None),
        now if ok else None,
        None if ok else now,
        values.get("error_class"),
        now,
        values.get("latency_ms"),
    ]
    sums = columns[6:18]
    updates = [f"{name}={name}+excluded.{name}" for name in sums]
    updates += [
        (
            "last_success_at=CASE WHEN excluded.last_success_at IS NOT NULL "
            "AND (last_success_at IS NULL OR excluded.last_success_at>last_success_at) "
            "THEN excluded.last_success_at ELSE last_success_at END"
        ),
        (
            "last_error_class=CASE WHEN excluded.last_error_at IS NOT NULL "
            "AND (last_error_at IS NULL OR excluded.last_error_at>=last_error_at) "
            "THEN excluded.last_error_class ELSE last_error_class END"
        ),
        (
            "last_error_at=CASE WHEN excluded.last_error_at IS NOT NULL "
            "AND (last_error_at IS NULL OR excluded.last_error_at>last_error_at) "
            "THEN excluded.last_error_at ELSE last_error_at END"
        ),
        (
            "last_latency_ms=CASE WHEN excluded.last_seen_at>=last_seen_at "
            "THEN excluded.last_latency_ms ELSE last_latency_ms END"
        ),
        "last_seen_at=max(last_seen_at,excluded.last_seen_at)",
    ]
    conn.execute(
        f"INSERT INTO usage_rollups({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)}) ON CONFLICT "
        f"(day,profile_id,credential_id,provider_id,model,plan_type) DO UPDATE SET {','.join(updates)}",
        data,
    )
