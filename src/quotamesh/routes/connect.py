"""Authorized Explain/Connect, Doctor, environment import and local activity."""

import asyncio
import hashlib
import json
import os
import re
import shlex
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quotamesh.config import registry
from quotamesh.connect import explain, integration_snippets
from quotamesh.credentials import CredentialCreate
from quotamesh.doctor import DoctorInput, run_doctor, until_disconnected
from quotamesh.envfile import (
    MAX_BYTES,
    EnvFileError,
    is_placeholder,
    parse_env,
    provider_for,
    suggested_plan,
)
from quotamesh.security import api_error, configuration_error, management_authorized
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


# JSON escaping can inflate 64 KiB of .env text; anything far beyond that is not a .env file.
MAX_ENVIRONMENT_REQUEST = 1024 * 1024
PLANS = ("FREE", "TRIAL_CREDIT", "PAID", "UNKNOWN")


class RequestTooLarge(Exception):
    """The request body exceeded its limit before it was fully read."""


class EnvironmentContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str


class EnvironmentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    plan_type: Literal["FREE", "TRIAL_CREDIT", "PAID", "UNKNOWN"] = "UNKNOWN"
    label: str | None = None
    quota_group: str | None = None


class EnvironmentFileImport(EnvironmentContent):
    store: Literal["value", "reference"] = "value"
    items: list[EnvironmentItem] = Field(min_length=1, max_length=200)


async def bounded_json(request, limit=MAX_ENVIRONMENT_REQUEST):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise RequestTooLarge
    return json.loads(body)


def inspect_environment_file(store, content):
    """Classify every entry; secret values stay inside this function and ``secrets``.

    Returns (entries, warnings, secrets) where ``secrets`` maps importable names to values
    for the caller's immediate use. Nothing here is logged or persisted.
    """
    if len(content.encode("utf-8", "surrogatepass")) > MAX_BYTES:
        raise RequestTooLarge
    parsed = parse_env(content)
    entries, values, seen = [], {}, set()
    for entry in sorted(parsed.entries.values(), key=lambda item: item.line):
        provider = provider_for(entry.name)
        value = entry.value.reveal()
        row = {
            "name": entry.name,
            "line": entry.line,
            "provider_id": provider,
            "status": "new",
            "fingerprint": None,
            "length": None,
            "suggested_plan": suggested_plan(provider) if provider else None,
        }
        if provider is None:
            row["status"] = "unsupported"
        elif is_placeholder(value):
            row["status"] = "placeholder"
        elif not re.fullmatch(r"[\x21-\x7e]+", value):
            # Keys travel in an HTTP header: whitespace or non-ASCII text cannot be a key.
            row["status"] = "invalid"
        else:
            row["fingerprint"], row["length"] = entry.value.fingerprint, len(entry.value)
            digest = hashlib.sha256(value.encode()).digest()
            if digest in seen:
                row["status"] = "duplicate"
            else:
                seen.add(digest)
                if store.configured_secret(provider, registry()[provider].base_url, value):
                    row["status"] = "configured"
                else:
                    values[entry.name] = value
        entries.append(row)
    warnings = [{"line": line, "message": message} for line, message in parsed.warnings]
    return entries, warnings, values


def environment_error(exc):
    """Map failures to fixed, content-free messages; values are never echoed."""
    if isinstance(exc, RequestTooLarge):
        return api_error(413, "The .env content is larger than 64 KiB", "request_too_large")
    if isinstance(exc, EnvFileError):
        return api_error(400, str(exc), "invalid_environment_file")
    if isinstance(exc, ValidationError):
        return configuration_error(exc)
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError, TypeError)):
        return api_error(400, "Send the .env text as a JSON string", "invalid_environment_file")
    return configuration_error(exc)


@router.post("/api/environment/parse")
async def parse_environment(request: Request):
    """Preview a pasted .env file: names and statuses only, never values."""
    if error := denied(request):
        return error
    try:
        values = EnvironmentContent.model_validate(await bounded_json(request))
        entries, warnings, _ = inspect_environment_file(request.app.state.store, values.content)
    except (RequestTooLarge, EnvFileError, ValueError, TypeError) as exc:
        return environment_error(exc)
    return JSONResponse({"entries": entries, "warnings": warnings})


@router.post("/api/environment/import-file")
async def import_environment_file(request: Request):
    """Save chosen keys from re-parsed .env text; the text itself is never stored."""
    if error := denied(request):
        return error
    store = request.app.state.store
    try:
        values = EnvironmentFileImport.model_validate(await bounded_json(request))
        entries, _, secrets = inspect_environment_file(store, values.content)
        statuses = {entry["name"]: entry for entry in entries}
        planned, skipped, warnings = [], [], []
        for item in values.items:
            entry = statuses.get(item.name)
            if entry is None:
                skipped.append({"name": item.name, "reason": "not_found"})
            elif any(item.name == name for name, _ in planned):
                skipped.append({"name": item.name, "reason": "duplicate"})
            elif item.name not in secrets:
                skipped.append({"name": item.name, "reason": entry["status"]})
            else:
                provider = entry["provider_id"]
                payload = {
                    "provider_id": provider,
                    "plan_type": item.plan_type,
                    "label": item.label or f"{registry()[provider].name} / {item.name}",
                    "quota_group": item.quota_group,
                }
                if values.store == "value":
                    payload["secret_value"] = secrets[item.name]
                else:
                    payload["env_name"] = item.name
                # Validate every item before saving any, so a bad label cannot half-import.
                planned.append((item.name, CredentialCreate.model_validate(payload).validated()))
    except (RequestTooLarge, EnvFileError, ValueError, TypeError) as exc:
        return environment_error(exc)
    imported = []
    for name, credential in planned:
        try:
            identifier = store.add_credential(**credential)
        except DuplicateCredential:
            skipped.append({"name": name, "reason": "configured"})
            continue
        imported.append(
            {
                "name": name,
                "credential_id": identifier,
                "provider_id": credential["provider_id"],
                "plan_type": credential["plan_type"],
            }
        )
        if values.store == "reference":
            current = os.environ.get(name)
            if not current:
                warnings.append(
                    {
                        "name": name,
                        "message": "Not set in the server environment; restart QuotaMesh "
                        "with this variable before routing to it.",
                    }
                )
            elif current != secrets[name]:
                warnings.append(
                    {
                        "name": name,
                        "message": "The server environment holds a different value; "
                        "requests use the server value.",
                    }
                )
    return JSONResponse(
        {"imported": imported, "skipped": skipped, "warnings": warnings}, status_code=201
    )


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
