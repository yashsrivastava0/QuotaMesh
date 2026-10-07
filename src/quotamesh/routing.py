"""Runtime snapshot adapter around the pure candidate function."""

import hashlib
import os
from datetime import UTC, datetime

from quotamesh.engine.select import candidates, quota_key


def decision_snapshot(app, slug="default"):
    snapshot = runtime_snapshot(app, slug)
    return snapshot["decisions"], snapshot["profile"], snapshot["credentials"]


def runtime_snapshot(app, slug="default"):
    """Read one state/clock snapshot; callers must never serialize its credentials."""
    now = datetime.now(UTC)
    profile, targets, credentials, states, usage = app.state.store.routing_snapshot(now, slug)
    for credential in credentials:
        credential["secret_available"] = bool(
            credential["secret_value"] or os.environ.get(credential["env_name"] or "")
        )
    identities = {}
    for credential in credentials:
        secret = credential["secret_value"] or os.environ.get(credential["env_name"] or "", "")
        if secret:
            identity = (
                credential["provider_id"],
                credential["base_url"],
                hashlib.sha256(secret.encode()).digest(),
            )
            identities.setdefault(identity, []).append(credential)
    # Legacy/environment aliases of one physical key cannot bypass its observed blocks.
    for aliases in identities.values():
        if len(aliases) < 2:
            continue
        groups = {quota_key(c) for c in aliases}
        blocked = [
            (model, state)
            for (group, model), state in list(states.items())
            if group in groups
            and state.get("status") in {"COOLDOWN", "EXHAUSTED"}
            and (state.get("until") is None or datetime.fromisoformat(state["until"]) > now)
        ]
        for model, state in sorted(blocked, key=lambda item: item[1].get("until") or "9999"):
            for group in groups:
                states[(group, model)] = state
        status = next(
            (c["status"] for c in aliases if c["status"] in {"INVALID", "UNUSABLE"}), None
        )
        if status:
            for c in aliases:
                c["status"] = status
    decisions = (
        candidates(profile, targets, credentials, states, usage, now, app.state.degraded)
        if profile
        else []
    )
    if app.state.accounting_failed:
        for decision in decisions:
            if decision["plan_type"] in {"PAID", "UNKNOWN"}:
                decision.update(eligible=False, skip_reason="accounting_unavailable")
    return {
        "evaluated_at": now.isoformat(),
        "decisions": decisions,
        "profile": profile,
        "targets": targets,
        "usage": usage,
        "credentials": {c["id"]: c for c in credentials},
    }
