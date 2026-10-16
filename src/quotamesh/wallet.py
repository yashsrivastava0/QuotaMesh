"""Wallet views built only from manual metadata and local gateway observations."""

import os
from datetime import datetime, timedelta

from quotamesh.engine.select import quota_key
from quotamesh.usage import summarize

DAILY_DAYS = 14


def daily_series(rows, today, days=DAILY_DAYS):
    """UTC days oldest first, zero-filled, from durable rollups (they outlive history)."""
    series = {}
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        series[day] = {
            "day": day,
            "routed_requests": 0,
            "attempts": 0,
            "failures": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "known_cost_usd": 0,
            "unknown_cost": 0,
            "by_provider": {},
        }
    for row in rows:
        entry = series.get(row["day"])
        if entry is None:
            continue
        for name in (
            "routed_requests",
            "attempts",
            "failures",
            "input_tokens",
            "output_tokens",
            "unknown_cost",
        ):
            entry[name] += row[name]
        cost = row["provider_cost_usd"] + row["estimated_cost_usd"]
        entry["known_cost_usd"] += cost
        provider = entry["by_provider"].setdefault(
            row["provider_id"], {"routed_requests": 0, "known_cost_usd": 0}
        )
        provider["routed_requests"] += row["routed_requests"]
        provider["known_cost_usd"] += cost
    for entry in series.values():
        # Sums of tiny per-call estimates otherwise carry binary float noise.
        entry["known_cost_usd"] = round(entry["known_cost_usd"], 10)
        for provider in entry["by_provider"].values():
            provider["known_cost_usd"] = round(provider["known_cost_usd"], 10)
    return list(series.values())


def snapshot(store, now):
    with store.connection() as conn:
        keys = [
            dict(r)
            for r in conn.execute(
                "SELECT c.*,s.env_name FROM credentials c LEFT JOIN secrets s ON c.id=s.credential_id ORDER BY c.priority,c.id"
            )
        ]
        states = [dict(r) for r in conn.execute("SELECT * FROM quota_state ORDER BY model")]
        targets = [dict(r) for r in conn.execute("SELECT * FROM profile_targets")]
        coverage = conn.execute("SELECT value FROM settings WHERE key='usage_coverage'").fetchone()[
            0
        ]
    profiles = store.profiles()
    rows = store.usage_rows()
    rates = store.rates(now)
    buckets = {"FREE": [], "TRIAL_CREDIT": [], "PAID": []}
    groups = {}
    for key in keys:
        groups.setdefault(quota_key(key), []).append(key)
    for group, all_keys in groups.items():
        active = [k for k in all_keys if not k["deleted_at"]]
        if not active:
            continue
        ids = {k["id"] for k in all_keys}
        source_rows = [
            r
            for r in rows
            if r["credential_id"] in ids and r["provider_id"] == active[0]["provider_id"]
        ]
        observed = summarize(source_rows)
        today = summarize([r for r in source_rows if r["day"] == now.date().isoformat()])
        models = [
            {"model": model, "usage": summarize([r for r in source_rows if r["model"] == model])}
            for model in sorted({r["model"] for r in source_rows})
        ]
        plans = {k["plan_type"] for k in active}
        bucket = (
            "PAID"
            if plans & {"PAID", "UNKNOWN"}
            else "TRIAL_CREDIT"
            if "TRIAL_CREDIT" in plans
            else "FREE"
        )
        credits = {k["starting_credit_usd"] for k in active if k["starting_credit_usd"] is not None}
        starting = next(iter(credits)) if len(credits) == 1 else None
        remaining = None
        if (
            starting is not None
            and not observed["unknown_cost"]
            and coverage.startswith("complete")
        ):
            remaining = max(0, starting - observed["known_cost_usd"])
        allocated = []
        for profile in profiles:
            if any(
                t["profile_id"] == profile["id"]
                and any(
                    t["provider_id"] == k["provider_id"]
                    and (t["credential_id"] is None or t["credential_id"] == k["id"])
                    for k in active
                )
                for t in targets
            ):
                allocated.append(
                    {
                        "slug": profile["slug"],
                        "name": profile["name"],
                        "enabled": bool(profile["enabled"]),
                    }
                )
        for key in active:
            expires = key["trial_expires_at"]
            key["expired"] = bool(expires and datetime.fromisoformat(expires) <= now)
            key["secret_available"] = not key["env_name"] or bool(os.environ.get(key["env_name"]))
            key["usage"] = summarize([r for r in source_rows if r["credential_id"] == key["id"]])
        current_states = [
            s
            | {
                "active": s["status"] in {"COOLDOWN", "EXHAUSTED"}
                and (not s["until"] or datetime.fromisoformat(s["until"]) > now)
            }
            for s in states
            if s["quota_group"] == group
        ]
        buckets[bucket].append(
            {
                "group": group,
                "provider_id": active[0]["provider_id"],
                "plan_types": sorted(plans),
                "shared": len(active) > 1,
                "keys": active,
                "allocated_profiles": allocated,
                "quota_states": current_states,
                "usage": observed,
                "today": today,
                "models": models,
                "starting_credit_usd": starting,
                "conflicting_credit": len(credits) > 1,
                "estimated_remaining_usd": remaining,
                "remaining_quota": None,
                "rate_observations": [r for r in rates if r["quota_group"] == group],
                "sources": {
                    "plan_types": "MANUAL",
                    "starting_credit_usd": "MANUAL",
                    "trial_expires_at": "MANUAL",
                    "estimated_remaining_usd": "LOCAL" if remaining is not None else "UNKNOWN",
                    "remaining_quota": "UNKNOWN",
                    "quota_states": "LOCAL",
                },
            }
        )
    return {
        "buckets": buckets,
        "totals": summarize(rows),
        "today": summarize([r for r in rows if r["day"] == now.date().isoformat()]),
        "daily": daily_series(rows, now.date()),
        "coverage": coverage,
        "profiles": profiles,
        "limitations": "Local observations only. Outside-gateway usage is invisible. Shared credit is counted once; remaining dollars are estimates, and provider quota is unknown.",
    }
