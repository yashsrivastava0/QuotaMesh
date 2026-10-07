"""Explicit, bounded provider diagnostics. No background probes or raw body storage."""

import asyncio
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime

import httpx
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from quotamesh.engine.classify import classify
from quotamesh.routes.gateway import execute_chat
from quotamesh.routing import decision_snapshot


class DoctorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_id: int = Field(ge=1)
    mode: str = Field(pattern="^(models|generation)$")
    profile: str = Field(default="default", pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=60)
    position: int | None = Field(default=None, ge=1, le=30)
    consent: bool = False


async def until_disconnected(request, operation):
    """Cancel provider work when a browser stops its diagnostic request."""
    stopping = asyncio.Event()

    async def disconnected():
        while not stopping.is_set():
            if await request.is_disconnected():
                return
            if stopping.is_set():
                return
            await asyncio.sleep(0.1)

    task = asyncio.create_task(operation)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return await task
        return None
    finally:
        stopping.set()
        task.cancel()
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await task
        with suppress(asyncio.CancelledError):
            await watcher


async def run_doctor(request, values):
    app = request.app
    _, _, credentials = decision_snapshot(app, values.profile)
    credential = credentials.get(values.credential_id)
    if credential is None:
        raise LookupError("Credential not found")
    if values.credential_id in app.state.doctor_running:
        raise RuntimeError("A check is already running for this credential")
    app.state.doctor_running.add(values.credential_id)
    started = time.monotonic()
    result = {
        "credential_id": credential["id"],
        "mode": values.mode,
        "checked_at": datetime.now(UTC).isoformat(),
        "outcome": "untested",
        "http_status": None,
        "models": [],
        "request_id": None,
        "source": "LOCAL",
        "models_source": "UNKNOWN",
    }
    try:
        if values.mode == "generation":
            if not values.consent or values.position is None:
                raise ValueError("Generation requires explicit quota consent and a saved target")
            decisions, profile, _ = decision_snapshot(app, values.profile)
            if profile is None:
                raise LookupError("Profile not found")
            chosen = next(
                (
                    d
                    for d in decisions
                    if (d["position"], d["credential_id"])
                    == (values.position, values.credential_id)
                ),
                None,
            )
            if not chosen:
                raise ValueError("Credential is not in this saved profile target")
            if not chosen["eligible"]:
                result["outcome"] = chosen["skip_reason"]
            else:
                payload = json.dumps(
                    {
                        "model": "qm/" + values.profile,
                        "messages": [{"role": "user", "content": "Reply OK."}],
                        "max_tokens": 16,
                        "stream": False,
                    }
                ).encode()
                scope = dict(request.scope)
                scope["headers"] = [
                    (b"authorization", ("Bearer " + app.state.local_key).encode()),
                    (b"content-type", b"application/json"),
                ]

                async def receive():
                    return {"type": "http.request", "body": payload, "more_body": False}

                result["request_id"] = uuid.uuid4().hex
                response = await execute_chat(
                    Request(scope, receive),
                    candidate=(values.position, values.credential_id),
                    request_id=result["request_id"],
                )
                result["http_status"] = response.status_code
                result["outcome"] = "ok" if response.status_code < 300 else "generation_failed"
                # Read only our metadata, never copy an upstream error/completion into diagnostics.
                if result["request_id"]:
                    attempt = next(
                        (
                            a
                            for a in app.state.store.recent_attempts()
                            if a["request_id"] == result["request_id"]
                        ),
                        None,
                    )
                    if attempt and attempt["error_class"]:
                        result["outcome"] = attempt["error_class"]
        else:
            secret = credential["secret_value"] or os.environ.get(credential["env_name"] or "")
            expired = credential["trial_expires_at"] and (
                datetime.fromisoformat(credential["trial_expires_at"]) <= datetime.now(UTC)
            )
            if not credential["enabled"] or expired or not secret:
                result["outcome"] = (
                    "disabled"
                    if not credential["enabled"]
                    else "expired"
                    if expired
                    else "missing_secret"
                )
            else:
                async with asyncio.timeout(10):
                    async with app.state.client.stream(
                        "GET",
                        credential["base_url"].rstrip("/") + "/models",
                        headers={"Authorization": "Bearer " + secret, "Accept": "application/json"},
                    ) as response:
                        result["http_status"] = response.status_code
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 1024 * 1024:
                                raise ValueError("Model list exceeded the diagnostic size limit")
                        if 300 <= response.status_code < 400:
                            result["outcome"] = "redirect_blocked"
                        elif response.status_code in {404, 405, 501}:
                            result["outcome"] = "listing_unsupported"
                        elif response.status_code >= 400:
                            outcome = classify(
                                credential["provider_id"],
                                response.status_code,
                                response.headers,
                                bytes(body),
                                datetime.now(UTC),
                            )
                            result["outcome"] = outcome.kind
                            # Model-list rate limits are not generation-model quota evidence.
                            if outcome.scope == "credential":
                                app.state.store.apply_outcome(
                                    {
                                        "credential_id": credential["id"],
                                        "credential_revision": credential["fingerprint"],
                                    },
                                    outcome,
                                    datetime.now(UTC),
                                )
                        else:
                            data = json.loads(body)
                            models = data.get("data") if isinstance(data, dict) else None
                            if not isinstance(models, list) or len(models) > 1000:
                                raise ValueError("Invalid model-list response")
                            ids = []
                            for model in models:
                                identifier = model.get("id") if isinstance(model, dict) else None
                                if (
                                    not isinstance(identifier, str)
                                    or not re.fullmatch(r"[A-Za-z0-9_.:/@+\-]{1,200}", identifier)
                                    or secret in identifier
                                ):
                                    raise ValueError("Invalid model identifier")
                                ids.append(identifier)
                            result.update(
                                outcome="listing_ok",
                                models=sorted(set(ids)),
                                models_source="PROVIDER",
                            )
                            with app.state.store.connection() as conn:
                                conn.execute(
                                    """UPDATE credentials SET status='ACTIVE',status_reason=NULL
                                    WHERE id=? AND fingerprint=? AND base_url=? AND deleted_at IS NULL
                                    AND status='INVALID'""",
                                    (
                                        credential["id"],
                                        credential["fingerprint"],
                                        credential["base_url"],
                                    ),
                                )
    except (TimeoutError, httpx.TimeoutException):
        result["outcome"] = "timeout"
    except httpx.HTTPError:
        result["outcome"] = "network_error"
    except sqlite3.Error:
        app.state.diagnostic_write_failures += 1
        result["outcome"] = "storage_error"
    except (json.JSONDecodeError, UnicodeDecodeError):
        result["outcome"] = "protocol_error"
    except ValueError:
        if values.mode == "generation":
            raise
        result["outcome"] = "protocol_error"
    finally:
        app.state.doctor_running.discard(values.credential_id)
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    result["notice"] = (
        "Listing access only; model generation access and remaining quota are not verified."
        if values.mode == "models"
        else "One target tested under the saved policy. Usage is included in gateway accounting."
    )
    try:
        result["saved"] = app.state.store.save_check(credential, result)
    except sqlite3.Error:
        app.state.diagnostic_write_failures += 1
        result["saved"] = False
    return result
