"""Dashboard, first-run setup, and local management API routes."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from quotamesh.config import registry, validate_base_url, validate_env_name
from quotamesh.engine.classify import valid_price
from quotamesh.policy import CredentialAction, Policy, expiry
from quotamesh.routing import decision_snapshot
from quotamesh.security import (
    api_error,
    browser_authorized,
    management_authorized,
)

router = APIRouter()
UI = Path(__file__).parents[1] / "ui"
templates = Jinja2Templates(directory=str(UI / "templates"))


def _dashboard_context(request: Request, *, error: str | None = None) -> dict[str, Any]:
    store = request.app.state.store
    return {
        "connection": store.connection_summary(),
        "attempts": store.recent_attempts(),
        "providers": registry(),
        "gateway_base": str(request.base_url).rstrip("/") + "/v1",
        "demo_mode": getattr(request.app.state, "demo_mode", False),
        "error": error,
        "routing": store.safe_routing(datetime.now(UTC)),
        "decisions": decision_snapshot(request.app)[0],
    }


def validate_connection(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate user-supplied connection fields before they reach persistence."""
    if not isinstance(payload, dict):
        raise TypeError("Expected a configuration object")
    provider_id = str(payload.get("provider_id", "")).strip()
    providers = registry()
    if provider_id not in providers:
        raise ValueError("Choose a supported provider preset.")
    plan = str(payload.get("plan_type", "")).strip()
    if plan not in {"FREE", "TRIAL_CREDIT", "PAID", "UNKNOWN"}:
        raise ValueError("Choose a valid plan type.")
    label = str(payload.get("label", "")).strip()
    model = str(payload.get("model", "")).strip()
    if (
        not label
        or len(label) > 100
        or not model
        or len(model) > 200
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in label + model)
    ):
        raise ValueError("Enter a label and upstream model (up to 100 and 200 characters).")
    secret_value = str(payload.get("secret_value", "")).strip() or None
    env_name = str(payload.get("env_name", "")).strip() or None
    if bool(secret_value) == bool(env_name):
        raise ValueError("Provide either an API secret or an environment variable name.")
    if env_name:
        validate_env_name(env_name)
    if provider_id == "custom":
        base_url = validate_base_url(str(payload.get("base_url", "")))
    else:
        base_url = validate_base_url(providers[provider_id].base_url)
    allow_paid = payload.get("allow_paid") in {True, "on", "true", "1"}
    quota_group = str(payload.get("quota_group", "")).strip() or None
    if quota_group and (len(quota_group) > 100 or any(ord(c) < 32 for c in quota_group)):
        raise ValueError("Quota group must be at most 100 printable characters")
    try:
        priority = int(payload.get("priority") or 0)
    except (ValueError, TypeError):
        raise ValueError("Priority must be an integer") from None
    if not -10000 <= priority <= 10000:
        raise ValueError("Priority must be between -10000 and 10000")
    prices = {}
    for name in ("input_price", "output_price"):
        value = payload.get(name)
        if value in (None, ""):
            prices[name] = None
        else:
            try:
                value = float(value)
            except (ValueError, TypeError):
                raise ValueError("Prices must be nonnegative USD per million tokens") from None
            if not valid_price(value) or value > 1_000_000:
                raise ValueError("Invalid token price")
            prices[name] = value
    return {
        **prices,
        "quota_group": quota_group,
        "priority": priority,
        "trial_expires_at": expiry(payload.get("trial_expires_at")),
        "provider_id": provider_id,
        "label": label,
        "plan_type": plan,
        "model": model,
        "base_url": base_url,
        "secret_value": secret_value,
        "env_name": env_name,
        "allow_paid": allow_paid if plan in {"PAID", "UNKNOWN"} else False,
    }


@router.get("/health")
def health() -> dict[str, str]:
    """Return process liveness without exposing configuration state."""
    return {"status": "ok"}


@router.get("/bootstrap")
def bootstrap(request: Request, token: str = "") -> Response:
    state = request.app.state
    if not state.bootstrap_token or not secrets.compare_digest(token, state.bootstrap_token):
        return api_error(403, "Bootstrap link is invalid or has already been used", "forbidden")
    state.bootstrap_token = None
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "qm_session", state.session_token, httponly=True, samesite="strict", path="/"
    )
    return response


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    if not browser_authorized(request):
        return HTMLResponse(
            "Local dashboard locked. Open the bootstrap URL printed by `quotamesh start`.",
            status_code=401,
        )
    return templates.TemplateResponse(request, "index.html", _dashboard_context(request))


@router.post("/connection")
async def save_connection_form(request: Request) -> Response:
    if not browser_authorized(request):
        return api_error(401, "Dashboard session required", "unauthorized")
    form = await request.form()
    try:
        values = validate_connection(dict(form))
        request.app.state.store.save_connection(**values)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            _dashboard_context(request, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse("/", status_code=303)


@router.get("/api/status")
def api_status(request: Request) -> Response:
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    store = request.app.state.store
    return JSONResponse(
        {
            "routing": store.safe_routing(datetime.now(UTC)),
            "decisions": decision_snapshot(request.app)[0],
            "connection": store.connection_summary(),
            "recent_attempts": store.recent_attempts(),
            "diagnostic_write_failures": request.app.state.diagnostic_write_failures,
        }
    )


@router.post("/api/connection")
async def api_connection(request: Request) -> Response:
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    try:
        values = validate_connection(await request.json())
        request.app.state.store.save_connection(**values)
    except (ValueError, TypeError, AttributeError) as exc:
        return api_error(400, str(exc), "invalid_configuration")
    return JSONResponse(
        {"connection": request.app.state.store.connection_summary()}, status_code=201
    )


@router.post("/api/credentials")
async def add_credential(request: Request):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    try:
        values = validate_connection(await request.json())
        identifier = request.app.state.store.add_credential(**values)
    except (ValueError, TypeError, AttributeError):
        return api_error(400, "Invalid credential configuration", "invalid_configuration")
    return JSONResponse({"credential_id": identifier}, status_code=201)


@router.post("/api/policy")
async def save_policy(request: Request):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    try:
        policy = Policy.model_validate(await request.json())
        credentials = request.app.state.store.routing_snapshot(datetime.now(UTC))[2]
        policy.validate_targets(credentials)
        values = policy.model_dump()
        targets = values.pop("targets")
        request.app.state.store.save_policy(values, targets)
    except (ValueError, TypeError, AttributeError):
        # ValidationError text can contain user input; keep the API response sanitized.
        return api_error(
            400,
            "Invalid policy: check targets, limits, and credential IDs",
            "invalid_configuration",
        )
    return JSONResponse({"saved": True})


@router.get("/api/dry-run")
def dry_run(request: Request):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    decisions, profile, _ = decision_snapshot(request.app)
    return JSONResponse(
        {
            "decisions": decisions,
            "selected": next((d for d in decisions if d["eligible"]), None),
            "configured": profile is not None,
        }
    )


@router.post("/api/credentials/{identifier}/action")
async def credential_action(identifier: int, request: Request):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    try:
        action = CredentialAction.model_validate(await request.json()).action
        store = request.app.state.store
        if action == "reset":
            store.reset_credential(identifier)
            request.app.state.degraded.clear()
        else:
            with store.connection() as conn:
                conn.execute(
                    "UPDATE credentials SET enabled=? WHERE id=?",
                    (int(action == "enable"), identifier),
                )
    except (ValueError, TypeError):
        return api_error(400, "Invalid credential action", "invalid_configuration")
    return JSONResponse({"saved": True})


@router.post("/api/demo")
async def demo_scenario(request: Request):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    if not getattr(request.app.state, "demo_mode", False):
        return api_error(403, "Scenarios are available only in the isolated demo", "demo_disabled")
    from quotamesh.demo import configure_demo

    try:
        scenario = (await request.json()).get("scenario")
        configure_demo(request.app, request.app.state.demo_base_url, scenario)
    except (ValueError, TypeError, AttributeError):
        return api_error(400, "Choose a supported demo scenario", "invalid_configuration")
    return JSONResponse({"saved": True})


@router.post("/api/test")
async def test_route(request: Request):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    from quotamesh.routes.gateway import chat_completions

    # Browser test uses the exact gateway handler; the local key never enters the page.
    scope = dict(request.scope)
    headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"authorization"]
    headers.append((b"authorization", ("Bearer " + request.app.state.local_key).encode()))
    scope["headers"] = headers
    return await chat_completions(Request(scope, request.receive))
