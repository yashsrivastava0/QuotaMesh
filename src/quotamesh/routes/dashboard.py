"""Dashboard, first-run setup, and local management API routes."""

from __future__ import annotations

import functools
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quotamesh.config import registry, validate_base_url, validate_env_name
from quotamesh.credentials import CredentialCreate
from quotamesh.engine.classify import valid_price
from quotamesh.policy import CredentialAction, Money, Policy, expiry
from quotamesh.routing import decision_snapshot
from quotamesh.security import (
    api_error,
    browser_authorized,
    configuration_error,
    consume_bootstrap_token,
    management_authorized,
    session_cookie_name,
)
from quotamesh.store import DuplicateCredential
from quotamesh.usage import summarize

router = APIRouter()
UI = Path(__file__).parents[1] / "ui"
templates = Jinja2Templates(directory=str(UI / "templates"))

# Navigation order; the title is also the navigation label.
PAGES = {
    "/": ("overview", "Overview", "Your API keys, today's usage, and what to do next."),
    "/start": (
        "start",
        "Get started",
        "Add an API key, check it, create qm/default, and send a first message.",
    ),
    "/access": ("access", "API keys", "Save provider API keys or environment references."),
    "/profiles": (
        "profiles",
        "Profiles",
        "Choose exact models, target order, and spending rules for each app.",
    ),
    "/playground": (
        "playground",
        "Playground",
        "Chat with a saved profile and see which provider answered, with tokens, timing and cost.",
    ),
    "/route": (
        "route",
        "Route preview",
        "See which target answers next and why others are skipped, without calling a provider.",
    ),
    "/connect": ("connect", "Connect", "Use one local endpoint with your app’s saved profile."),
    "/activity": (
        "activity",
        "Activity",
        "Follow requests and fallback attempts. Only metadata is retained.",
    ),
    "/help/doctor": (
        "doctor",
        "Diagnostics",
        "List a key's models or explicitly test one saved target.",
    ),
    "/help/catalog": (
        "catalog",
        "Providers",
        "Official documentation and access links from the dated registry.",
    ),
    "/help": ("help", "Help", "How QuotaMesh works, what the numbers mean, and the words used."),
}


@functools.lru_cache(maxsize=64)
def _asset_digest(name: str, mtime_ns: int, size: int) -> str:
    # Keyed by modification time and size, so an edited asset gets a new digest
    # without hashing every bundled file on every page render.
    return hashlib.sha256((UI / "static" / name).read_bytes()).hexdigest()[:12]


def asset_url(name: str) -> str:
    info = (UI / "static" / name).stat()
    return f"/static/{name}?v={_asset_digest(name, info.st_mtime_ns, info.st_size)}"


def _dashboard_context(request: Request, *, error: str | None = None) -> dict[str, Any]:
    store = request.app.state.store
    slug = request.query_params.get("profile", "default")
    page, title, description = PAGES.get(request.url.path, PAGES["/"])
    profiles = store.profiles()
    routing = store.safe_routing(datetime.now(UTC), slug)
    return {
        "page": page,
        "page_title": title,
        "page_description": description,
        "page_nav": [(path, values[1]) for path, values in PAGES.items()],
        # "Get started" stays prominent until a profile can route (named profiles need targets).
        "configured": bool(routing["targets"]) or any(p["slug"] != "default" for p in profiles),
        "selected_profile": slug,
        "profiles": profiles,
        "asset_url": asset_url,
        "demo_scenarios": [(key, values[0]) for key, values in _demo_scenarios().items()],
        "demo_scenario": getattr(request.app.state, "demo_scenario", "wallet"),
        "demo_guides": _demo_guides(),
        "providers": registry(),
        "gateway_base": str(request.base_url).rstrip("/") + "/v1",
        "demo_mode": getattr(request.app.state, "demo_mode", False),
        "error": error,
        "routing": routing,
        "decisions": decision_snapshot(request.app, slug)[0] if page == "overview" else [],
    }


def _demo_scenarios():
    from quotamesh.demo import SCENARIOS

    return SCENARIOS


def _demo_guides():
    from quotamesh.demo import GUIDES

    return GUIDES


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


LINK_USED = (
    "This login link was already used or has expired. "
    "Run <code>quotamesh open</code> to get a new one."
)
DASHBOARD_LOCKED = (
    "This browser is not signed in to the local dashboard, or its session ended when "
    "QuotaMesh restarted. Run <code>quotamesh open</code> to get a new login link."
)


def login_page(status: int, message: str) -> HTMLResponse:
    """A small static page (no scripts or inline styles) with the way back in."""
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Sign in · QuotaMesh</title></head><body><main>"
        f"<h1>QuotaMesh</h1><p>{message}</p></main></body></html>",
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/bootstrap")
def bootstrap(request: Request, token: str = "") -> Response:
    if browser_authorized(request):
        # Already signed in: keep the link usable (for example in another browser).
        return RedirectResponse("/", status_code=303)
    if not consume_bootstrap_token(request.app, token):
        return login_page(403, LINK_USED)
    response = RedirectResponse(
        "/", status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    )
    response.set_cookie(
        session_cookie_name(request),
        request.app.state.session_token,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@router.get("/", response_class=HTMLResponse)
@router.get("/start", response_class=HTMLResponse)
@router.get("/access", response_class=HTMLResponse)
@router.get("/profiles", response_class=HTMLResponse)
@router.get("/playground", response_class=HTMLResponse)
@router.get("/route", response_class=HTMLResponse)
@router.get("/connect", response_class=HTMLResponse)
@router.get("/activity", response_class=HTMLResponse)
@router.get("/help", response_class=HTMLResponse)
@router.get("/help/doctor", response_class=HTMLResponse)
@router.get("/help/catalog", response_class=HTMLResponse)
def home(request: Request) -> Response:
    if not browser_authorized(request):
        return login_page(401, DASHBOARD_LOCKED)
    slug = request.query_params.get("profile", "default")
    if slug != "default" and not any(p["slug"] == slug for p in request.app.state.store.profiles()):
        return HTMLResponse(
            'Profile not found. <a href="/profiles">Choose a saved profile</a>.', status_code=404
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
def api_status(request: Request, profile: str = "default") -> Response:
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    store = request.app.state.store
    return JSONResponse(
        {
            "routing": store.safe_routing(datetime.now(UTC), profile),
            "decisions": decision_snapshot(request.app, profile)[0],
            "connection": store.connection_summary(),
            "recent_attempts": store.recent_attempts(),
            "diagnostic_write_failures": request.app.state.diagnostic_write_failures,
            "observed_today": summarize(
                store.usage_rows(
                    profile_id=(
                        store.safe_routing(datetime.now(UTC), profile)["profile"] or {}
                    ).get("id", -1),
                    day=datetime.now(UTC).date().isoformat(),
                )
            ),
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
        values = CredentialCreate.model_validate(await request.json()).validated()
        identifier = request.app.state.store.add_credential(**values)
    except DuplicateCredential as exc:
        return api_error(409, str(exc), "duplicate_credential")
    except (ValueError, TypeError, AttributeError) as exc:
        return configuration_error(exc)
    return JSONResponse({"credential_id": identifier}, status_code=201)


class InitialSetup(BaseModel):
    """First-run default profile. Spend fields use the Policy limits; paid stays off by default."""

    model_config = ConfigDict(extra="forbid")
    credential_id: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=200)
    allow_paid: bool = False
    paid_daily_cap_usd: Money | None = None
    paid_monthly_cap_usd: Money | None = None
    input_price: Money | None = None  # USD per million tokens, for the created target
    output_price: Money | None = None
    allow_unknown_price: bool = False


@router.post("/api/setup")
async def initial_setup(request: Request):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    try:
        values = InitialSetup.model_validate(await request.json())
        if any(ord(c) < 32 or ord(c) == 127 for c in values.model):
            raise TypeError("Invalid model")
        request.app.state.store.setup_default(
            values.credential_id, values.model, **values.model_dump(exclude={"credential_id", "model"})
        )
    except LookupError:
        return api_error(404, "Credential not found", "credential_not_found")
    except ValidationError as exc:
        return configuration_error(exc)  # Field paths and fixed advice; inputs never echoed.
    except (TypeError, AttributeError):
        return api_error(400, "Choose a saved credential and model", "invalid_configuration")
    except ValueError as exc:
        if str(exc).startswith("Default is already"):
            return api_error(409, str(exc), "setup_conflict")
        return api_error(400, "Choose a saved credential and model", "invalid_configuration")
    return JSONResponse({"alias": "qm/default", "allow_paid": values.allow_paid}, status_code=201)


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
def dry_run(request: Request, profile: str = "default"):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")
    decisions, profile, _ = decision_snapshot(request.app, profile)
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
        credential = next(
            (
                c
                for c in store.safe_routing(datetime.now(UTC))["credentials"]
                if c["id"] == identifier
            ),
            None,
        )
        if credential is None:
            return api_error(404, "Credential not found", "credential_not_found")
        if action == "reset":
            store.reset_credential(identifier)
            request.app.state.degraded = {
                key: until
                for key, until in request.app.state.degraded.items()
                if key[0] != credential["provider_id"] or (len(key) == 3 and key[2] != identifier)
            }
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
    return JSONResponse({"saved": True, "scenario": scenario})


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
