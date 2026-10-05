"""The shared live/dry-run candidate selector."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def quota_key(credential: dict[str, Any]) -> str:
    # Namespace user groups by provider; missing groups conservatively share provider capacity.
    return (
        credential["provider_id"]
        + ":"
        + (
            credential.get("quota_group")
            or (str(credential["id"]) if credential["provider_id"] == "groq" else "default")
        )
    )


def candidates(profile, targets, credentials, states, usage, now, degraded=None):
    decisions = []
    degraded = degraded or {}
    for target in sorted(targets, key=lambda t: t["position"]):
        pool = sorted(
            [
                c
                for c in credentials
                if c["provider_id"] == target["provider_id"]
                and (target["credential_id"] is None or c["id"] == target["credential_id"])
            ],
            key=lambda c: (c["priority"], c["id"]),
        )
        if not pool:
            decisions.append(
                {
                    "position": target["position"],
                    "provider_id": target["provider_id"],
                    "model": target["model"],
                    "credential_id": None,
                    "label": "Provider pool",
                    "plan_type": "UNKNOWN",
                    "quota_group": None,
                    "eligible": False,
                    "skip_reason": "no_credentials",
                    "recovery_at": None,
                    "input_price": target.get("input_price"),
                    "output_price": target.get("output_price"),
                }
            )
        for c in pool:
            reason, recovery = None, None
            state = states.get((quota_key(c), target["model"]), {})
            transient = max(
                filter(
                    None,
                    (
                        degraded.get((c["provider_id"], target["model"])),
                        degraded.get((c["provider_id"], target["model"], c["id"])),
                    ),
                ),
                default=None,
            )
            expiry = c.get("trial_expires_at")
            paid = c["plan_type"] in {"PAID", "UNKNOWN"}
            if not profile["enabled"] or not c["enabled"]:
                reason = "disabled"
            elif expiry and datetime.fromisoformat(expiry) <= now:
                reason = "expired"
            elif c["status"] != "ACTIVE":
                reason = c["status"].lower()
            elif c["plan_type"] == "TRIAL_CREDIT" and not profile["allow_trial"]:
                reason = "trial_blocked"
            elif paid and not profile["allow_paid"]:
                reason = "paid_blocked"
            elif not c.get("secret_available", True):
                reason = "missing_secret"
            elif state.get("status") in {"COOLDOWN", "EXHAUSTED"} and (
                state.get("until") is None or datetime.fromisoformat(state["until"]) > now
            ):
                reason, recovery = state["status"].lower(), state.get("until")
            elif transient and transient > now:
                reason, recovery = "degraded", transient.isoformat()
            elif paid:
                caps = [
                    ("daily", profile["paid_daily_cap_usd"]),
                    ("monthly", profile["paid_monthly_cap_usd"]),
                ]
                if any(cap is not None and usage.get(period, 0) >= cap for period, cap in caps):
                    reason = "cap_reached"
                elif any(cap is not None for _, cap in caps) and not profile["allow_unknown_price"]:
                    if any(
                        cap is not None
                        and usage.get("unknown_" + period, usage.get("unknown", False))
                        for period, cap in caps
                    ):
                        reason = "unknown_spend"
                    elif target.get("input_price") is None or target.get("output_price") is None:
                        reason = "unknown_price"
            decisions.append(
                {
                    "position": target["position"],
                    "provider_id": c["provider_id"],
                    "model": target["model"],
                    "credential_id": c["id"],
                    "label": c["label"],
                    "plan_type": c["plan_type"],
                    "quota_group": quota_key(c),
                    "eligible": reason is None,
                    "skip_reason": reason,
                    "recovery_at": recovery,
                    "input_price": target.get("input_price"),
                    "output_price": target.get("output_price"),
                }
            )
    return decisions
