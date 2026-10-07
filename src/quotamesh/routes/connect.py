"""Authorized Explain/Connect, Doctor, environment import and local activity."""

import asyncio
import json
import os
import shlex
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from quotamesh.config import registry
from quotamesh.connect import explain, integration_snippets
from quotamesh.credentials import CredentialCreate
from quotamesh.doctor import DoctorInput, run_doctor, until_disconnected
from quotamesh.security import api_error, management_authorized
from quotamesh.store import DuplicateCredential

router = APIRouter()
ENV_NAMES = {
    "OPENAI_API_KEY": "openai",
    "GEMINI_API_KEY": "gemini",
    "GOOGLE_API_KEY": "gemini",
    "GROQ_API_KEY": "groq",
    "NVIDIA_API_KEY": "nim",
}


def denied(request):
    if not management_authorized(request):
        return api_error(401, "Local authorization required", "unauthorized")


@router.get("/api/explain")
def explanation(request: Request, profile: str = "default"):
    if error := denied(request):
        return error
    try:
        return JSONResponse(explain(request.app, profile))
    except LookupError:
        return api_error(404, "Profile not found", "profile_not_found")


@router.get("/api/integrations")
def integrations(
    request: Request, profile: str = "default", shell: Literal["bash", "powershell"] = "bash"
):
    if error := denied(request):
        return error
    chosen = next((p for p in request.app.state.store.profiles() if p["slug"] == profile), None)
    if chosen is None and profile != "default":
        return api_error(404, "Profile not found", "profile_not_found")
    base = str(request.base_url).rstrip("/") + "/v1"
    result = integration_snippets(base, profile, shell, (chosen or {}).get("name"))
    result["enabled"] = bool(chosen and chosen["enabled"])
    result["configured"] = chosen is not None
    if not result["configured"]:
        result["notice"] += " Save the default profile before sending a request."
    result["demo"] = bool(getattr(request.app.state, "demo_mode", False))
    if result["demo"]:
        directory = str(request.app.state.store.data_dir)
        quoted = (
            "'" + directory.replace("'", "''") + "'"
            if shell == "powershell"
            else shlex.quote(directory)
        )
        result["snippets"]["env"] = result["snippets"]["env"].replace(
            "quotamesh key", "quotamesh key --data-dir " + quoted
        )
        result["notice"] += (
            " Temporary demo: the environment snippet reads its isolated key. Data disappears on exit."
        )
    return JSONResponse(result)


@router.get("/api/doctor")
def doctor_results(request: Request):
    if error := denied(request):
        return error
    return JSONResponse({"checks": request.app.state.store.checks(datetime.now(UTC))})


@router.post("/api/doctor")
async def doctor(request: Request):
    if error := denied(request):
        return error
    try:
        values = DoctorInput.model_validate(await request.json())
        result = await until_disconnected(request, run_doctor(request, values))
        return JSONResponse(result) if result is not None else Response(status_code=499)
    except LookupError:
        return api_error(404, "Credential or profile not found", "not_found")
    except RuntimeError:
        return api_error(409, "A check is already running for this credential", "check_running")
    except (ValueError, TypeError):
        return api_error(
            400,
            "Check fields; generation needs quota consent and a saved target",
            "invalid_diagnostic",
        )


@router.get("/api/catalog")
def catalog(request: Request):
    if error := denied(request):
        return error
    path = Path(__file__).parents[1] / "registry" / "catalog.toml"
    rows = tomllib.loads(path.read_text(encoding="utf-8"))["entries"]
    credentials = request.app.state.store.safe_routing(datetime.now(UTC))["credentials"]
    for row in rows:
        row["configured"] = any(c["provider_id"] == row["id"] for c in credentials)
        row["preset"] = row["id"] in registry()
    return JSONResponse(
        {
            "entries": rows,
            "notice": "Access programs change. Follow provider terms; no exact free limits are promised.",
        }
    )


def environment_preview(request):
    credentials = request.app.state.store.safe_routing(datetime.now(UTC))["credentials"]
    return [
        {
            "env_name": name,
            "provider_id": provider,
            "present": bool(os.environ.get(name)),
            "configured": any(
                c["env_name"] == name and c["provider_id"] == provider for c in credentials
            ),
        }
        for name, provider in ENV_NAMES.items()
    ]


@router.get("/api/environment")
def environment(request: Request):
    if error := denied(request):
        return error
    return JSONResponse(
        {
            "variables": environment_preview(request),
            "notice": "Only the running server environment is inspected. Values are never returned.",
        }
    )


class EnvironmentImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    names: list[str] = Field(min_length=1, max_length=5)
    plan_type: Literal["FREE", "TRIAL_CREDIT", "PAID", "UNKNOWN"] = "UNKNOWN"


@router.post("/api/environment/import")
async def import_environment(request: Request):
    if error := denied(request):
        return error
    try:
        values = EnvironmentImport.model_validate(await request.json())
        if any(name not in ENV_NAMES for name in values.names):
            raise ValueError("Unsupported variable")
    except (ValueError, TypeError):
        return api_error(
            400, "Choose known provider variable names and a valid plan", "invalid_configuration"
        )
    preview = {v["env_name"]: v for v in environment_preview(request)}
    imported, skipped = [], []
    for name in dict.fromkeys(values.names):
        row = preview[name]
        if not row["present"] or row["configured"]:
            skipped.append(name)
            continue
        credential = CredentialCreate(
            provider_id=row["provider_id"],
            plan_type=values.plan_type,
            label=registry()[row["provider_id"]].name + " / " + name,
            env_name=name,
        ).validated()
        try:
            identifier = request.app.state.store.add_credential(**credential)
        except DuplicateCredential:
            skipped.append(name)
            continue
        imported.append({"env_name": name, "credential_id": identifier})
    return JSONResponse({"imported": imported, "skipped": skipped})


@router.get("/events")
async def events(request: Request):
    if error := denied(request):
        return error
    feed = request.app.state.activity
    queue = feed.subscribe()
    if queue is None:
        return api_error(503, "Too many activity subscribers", "subscriber_limit")

    async def stream():
        try:
            yield 'event: refresh\ndata: {"refresh":true}\n\n'
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"id: {event['id']}\nevent: activity\ndata: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            feed.subscribers.discard(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
