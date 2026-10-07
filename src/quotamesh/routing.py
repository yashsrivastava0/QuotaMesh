"""Runtime snapshot adapter around the pure candidate function."""

import os
from datetime import UTC, datetime

from quotamesh.engine.select import candidates


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
