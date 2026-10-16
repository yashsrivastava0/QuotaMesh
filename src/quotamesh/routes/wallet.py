"""Authorized profile, wallet, usage, and metadata history management."""

import json
import re
from datetime import UTC, date, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from quotamesh.connect import REASONS
from quotamesh.credentials import CredentialPatch
from quotamesh.policy import ProjectProfile
from quotamesh.security import api_error, configuration_error, management_authorized
from quotamesh.usage import summarize
from quotamesh.wallet import snapshot

router = APIRouter()
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
TRACE_FIELDS = (
    "attempt_idx",
    "ts",
    "provider_id",
    "model",
    "credential_id",
    "credential_label",
    "plan_type",
    "outcome",
    "http_status",
    "error_class",
    "latency_ms",
    "ttfb_ms",
    "input_tokens",
    "output_tokens",
    "provider_cost_usd",
    "estimated_cost_usd",
    "streamed",
)


def denied(request):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    return None


def invalid():
    return api_error(
        400,
        "Invalid configuration: check fields, limits, and target credentials",
        "invalid_configuration",
    )


@router.get("/api/wallet")
def wallet(request: Request):
    if error := denied(request):
        return error
    return JSONResponse(snapshot(request.app.state.store, datetime.now(UTC)))


@router.get("/api/profiles")
def profiles(request: Request):
    if error := denied(request):
        return error
    store = request.app.state.store
    return JSONResponse(
        {
            "profiles": [
                store.safe_routing(datetime.now(UTC), p["slug"]) | {"alias": "qm/" + p["slug"]}
                for p in store.profiles()
            ]
        }
    )


async def save(request, slug=None):
    if error := denied(request):
        return error
    store = request.app.state.store
    try:
        profile = ProjectProfile.model_validate(await request.json())
        if slug is not None and profile.slug != slug:
            return api_error(400, "Profile slugs are immutable", "immutable_slug")
        if slug is None and profile.slug == "default":
            return api_error(409, "Default already exists; edit its policy", "profile_conflict")
        profile.validate_targets(store.routing_snapshot(datetime.now(UTC))[2])
        values = profile.model_dump()
        targets, name, name_slug = values.pop("targets"), values.pop("name"), values.pop("slug")
        identifier = store.save_profile(name_slug, values, targets, name=name, create=slug is None)
    except LookupError:
        return api_error(404, "Profile not found", "profile_not_found")
    except (ValidationError, TypeError, AttributeError) as exc:
        return configuration_error(exc)
    except ValueError as exc:
        return api_error(409, str(exc), "profile_conflict")
    return JSONResponse(
        {"profile_id": identifier, "alias": "qm/" + name_slug},
        status_code=201 if slug is None else 200,
    )


@router.post("/api/profiles")
async def create_profile(request: Request):
    return await save(request)


@router.put("/api/profiles/{slug}")
async def edit_profile(slug: str, request: Request):
    return await save(request, slug)


@router.delete("/api/profiles/{slug}")
def delete_profile(slug: str, request: Request):
    if error := denied(request):
        return error
    try:
        request.app.state.store.delete_profile(slug)
    except LookupError:
        return api_error(404, "Profile not found", "profile_not_found")
    except ValueError as exc:
        return api_error(409, str(exc), "profile_conflict")
    return JSONResponse({"deleted": True})


@router.patch("/api/credentials/{identifier}")
async def edit_credential(identifier: int, request: Request):
    if error := denied(request):
        return error
    store = request.app.state.store
    existing = next(
        (c for c in store.safe_routing(datetime.now(UTC))["credentials"] if c["id"] == identifier),
        None,
    )
    if not existing:
        return api_error(404, "Credential not found", "credential_not_found")
    try:
        values = CredentialPatch.model_validate(await request.json()).validated(existing)
        store.edit_credential(identifier, values)
    except (ValidationError, TypeError, AttributeError) as exc:
        return configuration_error(exc)
    except LookupError:
        return api_error(404, "Credential not found", "credential_not_found")
    except ValueError as exc:
        return api_error(409, str(exc), "credential_conflict")
    return JSONResponse({"saved": True})


@router.delete("/api/credentials/{identifier}")
def delete_credential(identifier: int, request: Request):
    if error := denied(request):
        return error
    try:
        request.app.state.store.delete_credential(identifier)
    except LookupError:
        return api_error(404, "Credential not found", "credential_not_found")
    except ValueError as exc:
        return api_error(409, str(exc), "credential_conflict")
    return JSONResponse({"deleted": True})


@router.get("/api/usage")
def usage(request: Request, profile: str | None = None, day: str | None = None):
    if error := denied(request):
        return error
    store = request.app.state.store
    chosen = None
    if profile:
        chosen = next(
            (p for p in store.profiles(include_archived=True) if p["slug"] == profile), None
        )
        if not chosen:
            return api_error(404, "Profile not found", "profile_not_found")
    if day:
        try:
            if date.fromisoformat(day).isoformat() != day:
                return invalid()
        except ValueError:
            return invalid()
    rows = store.usage_rows(profile_id=chosen["id"] if chosen else None, day=day)
    return JSONResponse(
        {"rows": rows, "totals": summarize(rows), "profile": profile, "day": day, "calendar": "UTC"}
    )


def skipped_candidates(raw):
    """Labels and fixed explanations of candidates skipped before an attempt."""
    try:
        decisions = json.loads(raw or "[]")
    except ValueError:
        return []
    return [
        {
            "label": d.get("label"),
            "skip_reason": d.get("skip_reason"),
            "explanation": REASONS.get(d.get("skip_reason"), "Skipped under the saved policy."),
        }
        for d in decisions
        if isinstance(decisions, list) and isinstance(d, dict)
    ]


def trace_view(rows):
    attempts = [
        {name: row.get(name) for name in TRACE_FIELDS}
        | {"skipped": skipped_candidates(row.get("skipped_json"))}
        for row in rows
    ]
    costs = [
        row["provider_cost_usd"]
        if row["provider_cost_usd"] is not None
        else row["estimated_cost_usd"]
        for row in rows
    ]
    if any(cost is None for cost in costs):
        source = "UNKNOWN"
    elif any(row["provider_cost_usd"] is None for row in rows):
        source = "LOCAL"
    else:
        source = "PROVIDER"

    def total(name):
        values = [row[name] for row in rows if row[name] is not None]
        return sum(values) if values else None

    return {
        "request_id": rows[0]["request_id"],
        "profile_slug": rows[0]["profile_slug"],
        "started_at": min(row["ts"] for row in rows),
        "attempts": attempts,
        "totals": {
            "attempts": len(rows),
            "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "known_cost_usd": sum(cost for cost in costs if cost is not None),
            "cost_source": source,
        },
    }


@router.get("/api/requests/{request_id}")
def request_trace(request_id: str, request: Request):
    """One request's ordered attempts: metadata only, never prompt or reply text."""
    if error := denied(request):
        return error
    rows = (
        request.app.state.store.request_trace(request_id)
        if REQUEST_ID.fullmatch(request_id)
        else []
    )
    if not rows:
        return api_error(404, "Request not found or no longer retained", "request_not_found")
    return JSONResponse(trace_view(rows))


@router.get("/api/activity")
def activity(
    request: Request, profile: str | None = None, limit: int = 20, before: int | None = None
):
    if error := denied(request):
        return error
    if not 1 <= limit <= 100 or (before is not None and before < 1):
        return invalid()
    return JSONResponse(request.app.state.store.activity(slug=profile, limit=limit, before=before))
