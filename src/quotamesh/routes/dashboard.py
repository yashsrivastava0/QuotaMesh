"""Dashboard, first-run setup, and local management API routes."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from quotamesh.config import registry, validate_base_url, validate_env_name
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
        "error": error,
    }


def validate_connection(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate user-supplied connection fields before they reach persistence."""
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
    return {
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
    response.set_cookie("qm_session", state.session_token, httponly=True, samesite="strict", path="/")
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
    return JSONResponse({"connection": request.app.state.store.connection_summary()}, status_code=201)

