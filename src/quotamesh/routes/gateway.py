"""OpenAI-compatible model gateway and upstream streaming transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from quotamesh.config import registry
from quotamesh.connect import REASONS, recovery_action
from quotamesh.engine.classify import (
    Outcome,
    classify,
    error_status,
    sse_payload,
    usage_metadata,
)
from quotamesh.observations import rate_observations
from quotamesh.routing import decision_snapshot
from quotamesh.security import api_error, bearer_authorized
from quotamesh.store import utc_now

router = APIRouter()
LOG = logging.getLogger("quotamesh")
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_FIRST_EVENT_BYTES = 64 * 1024
# An injected stream is forwarded event by event; this bounds bytes held awaiting an event end.
MAX_FRAMED_BYTES = 1024 * 1024
# A committed stream fails after this long without upstream bytes (or the profile's longer
# first-event timeout); httpx itself has no read timeout.
MIN_IDLE_TIMEOUT_S = 60.0
# OpenAI-shaped "created" for profile aliases: one stable epoch per process.
MODELS_CREATED = int(time.time())
STREAM_ENDED = (
    b'event: error\ndata: {"error":{"message":"Upstream ended before DONE",'
    b'"type":"upstream_stream"}}\n\n'
)
STREAM_INTERRUPTED = (
    b'event: error\ndata: {"error":{"message":"Upstream stream interrupted",'
    b'"type":"upstream_stream"}}\n\n'
)


def _sse_end(buffer: bytes) -> int:
    points = [pos for marker in (b"\n\n", b"\r\n\r\n") if (pos := buffer.find(marker)) >= 0]
    if not points:
        return -1
    pos = min(points)
    return pos + (4 if buffer[pos : pos + 4] == b"\r\n\r\n" else 2)


async def first_sse_event(chunks: AsyncIterator[bytes], timeout_s: float) -> tuple[bytes, bytes]:
    """Read one bounded SSE event before committing the downstream response."""
    buffer = b""
    async with asyncio.timeout(timeout_s):
        async for chunk in chunks:
            buffer += chunk
            end = _sse_end(buffer)
            if end >= 0:
                if end > MAX_FIRST_EVENT_BYTES:
                    raise ValueError("First streaming event is too large")
                return buffer[:end], buffer[end:]
            if len(buffer) > MAX_FIRST_EVENT_BYTES:
                raise ValueError("First streaming event is too large")
    raise ValueError("Upstream ended before the first streaming event")


def first_event_is_error(event: bytes) -> bool:
    if any(line.strip().lower() == b"event: error" for line in event.splitlines()):
        return True
    for line in event.splitlines():
        if line.startswith(b"data:"):
            try:
                payload = json.loads(line[5:].strip())
            except (ValueError, UnicodeDecodeError):
                return False
            return isinstance(payload, dict) and "error" in payload
    return False


def _record_attempt(request: Request, **values: Any) -> None:
    try:
        request.app.state.store.record_attempt(**values)
        request.app.state.activity.publish(values)
    except sqlite3.Error:
        request.app.state.diagnostic_write_failures += 1
        if values.get("paid"):
            request.app.state.accounting_failed = True
            # Retain a safety marker across restarts if the independent filesystem is writable.
            try:
                request.app.state.store.accounting_marker.touch(mode=0o600, exist_ok=True)
            except OSError:
                LOG.warning("Could not persist paid-accounting safety marker")
        LOG.warning("Could not write request metadata")


class StreamState:
    """Metadata observed on a committed stream; never holds content."""

    __slots__ = ("done", "error", "finished", "usage")

    def __init__(self) -> None:
        self.done = self.error = self.finished = False
        self.usage: dict[str, Any] = {}


def inspect_event(event: bytes, state: StreamState, decision: dict[str, Any]) -> bool:
    """Record metadata from one complete SSE event; return True for a usage-only chunk.

    Inspection is bounded to small events, keeps token/cost metadata only and never changes
    the bytes. A usage-only chunk is OpenAI's final `"choices": []` event carrying usage.
    """
    if len(event) > MAX_FIRST_EVENT_BYTES:
        return False
    data = b"\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:"))
    if data.strip() == b"[DONE]":
        state.done = True
        return False
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(obj, dict):
        return False
    if "error" in obj:
        state.error = True
        return False
    usage = obj.get("usage")
    if usage and isinstance(usage, dict):
        state.usage = usage_metadata(data, decision)
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return False
    if any(isinstance(choice, dict) and choice.get("finish_reason") for choice in choices):
        state.finished = True
    return not choices and isinstance(usage, dict)


async def relay_stream(
    request: Request,
    *,
    first: bytes,
    remainder: bytes,
    chunks: AsyncIterator[bytes],
    upstream: httpx.Response,
    selected: dict[str, Any],
    details: dict[str, Any],
    started: float,
    framed: bool,
    idle_s: float,
) -> AsyncIterator[bytes]:
    """Forward a committed stream; a failure after commitment becomes an SSE error event.

    Without injected usage, upstream chunks pass through byte for byte. When QuotaMesh injected
    `stream_options.include_usage` (`framed`), complete events are forwarded unchanged except
    the usage-only chunk the client did not ask for.
    """
    state = StreamState()
    result, error_class = "OK", None
    pending = [part for part in (first, remainder) if part]
    # Framed: bytes awaiting an event end. Otherwise: a bounded copy used only for inspection.
    buffer = b""
    eof = False
    try:
        while not state.error:
            if pending:
                chunk = pending.pop(0)
            else:
                try:
                    # A stalled upstream becomes a mid-stream failure (TimeoutError below).
                    async with asyncio.timeout(idle_s):
                        chunk = await anext(chunks)
                except StopAsyncIteration:
                    eof = True
                    break
            buffer += chunk
            forward = []
            while (end := _sse_end(buffer)) >= 0:
                event, buffer = buffer[:end], buffer[end:]
                usage_only = inspect_event(event, state, selected)
                if framed and not usage_only:
                    forward.append(event)
            if not framed:
                if len(buffer) > MAX_FIRST_EVENT_BYTES:
                    buffer = b""
                yield chunk
                continue
            if len(buffer) > MAX_FRAMED_BYTES:
                # No event end within the bound: stop stripping and pass the rest through raw.
                forward.append(buffer)
                buffer, framed = b"", False
            if forward:
                yield b"".join(forward)
        if eof and buffer:
            inspect_event(buffer, state, selected)  # a final event may lack its blank line
            if framed:
                yield buffer
        # Complete: [DONE], or a finish_reason followed by a clean upstream close (some
        # providers omit the marker; docs/live-provider-notes.md D1).
        if state.error or not (state.done or (state.finished and eof)):
            result, error_class = "MID_STREAM_FAILURE", "stream"
            if not state.error:
                yield STREAM_ENDED
    except (asyncio.CancelledError, GeneratorExit):
        result, error_class = "CLIENT_CANCELLED", "cancelled"
        raise
    except (httpx.HTTPError, TimeoutError):
        if not state.done:  # after [DONE] the response is already complete
            result, error_class = "MID_STREAM_FAILURE", "stream"
            yield STREAM_INTERRUPTED
    finally:
        if selected["plan_type"] == "FREE" and state.usage.get("provider_cost_usd") is None:
            state.usage["estimated_cost_usd"] = 0
        try:
            # Synchronous, before any await, so cancellation cannot skip the record.
            _record_attempt(
                request,
                **details,
                **state.usage,
                outcome=result,
                http_status=200,
                error_class=error_class,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            await upstream.aclose()


def deadline_response(attempts, last_error_class=None, last_http_status=None, headers=None):
    """Contract 504 once the profile deadline passes; describes the last attempt, if any."""
    return JSONResponse(
        {
            "error": {
                "message": "Route attempt deadline exceeded",
                "type": "deadline_exceeded",
                "code": "deadline_exceeded",
            },
            "quotamesh": {
                "attempts": attempts,
                "last_error_class": last_error_class,
                "last_http_status": last_http_status,
            },
        },
        status_code=504,
        headers=headers if attempts else None,
    )


def blocked_response(decisions, code="no_candidates", slug="default", request_id=None):
    now = datetime.now(UTC)
    recoveries = [datetime.fromisoformat(d["recovery_at"]) for d in decisions if d["recovery_at"]]
    wait = min(((date - now).total_seconds() for date in recoveries), default=9999)
    status = 429 if 0 < wait <= 60 else 503
    headers = {"Retry-After": str(max(1, math.ceil(wait)))} if status == 429 else {}
    headers["X-QuotaMesh-Profile"] = slug
    if request_id:
        headers["X-QuotaMesh-Request-Id"] = request_id
    # The same explanation and recovery wording as Explain, so clients can say what to fix.
    candidates = [
        {
            **d,
            "explanation": REASONS.get(d["skip_reason"], "Eligible under the saved policy."),
            "recovery_action": None if d["eligible"] else recovery_action(d, slug),
        }
        for d in decisions
    ]
    return JSONResponse(
        {
            "error": {"message": f"No eligible capacity for qm/{slug}", "type": code, "code": code},
            "quotamesh": {"candidates": candidates},
        },
        status_code=status,
        headers=headers,
    )


def apply_outcome(request, decision, outcome):
    now = datetime.now(UTC)
    if outcome.scope in {"target", "credential_model"}:
        key = (decision["provider_id"], decision["model"])
        if outcome.scope == "credential_model":
            key += (decision["credential_id"],)
        request.app.state.degraded[key] = now + timedelta(seconds=30)
    try:
        request.app.state.store.apply_outcome(decision, outcome, now)
    except sqlite3.Error:
        request.app.state.diagnostic_write_failures += 1
        # A failed safety-state write must not silently reopen the source.
        request.app.state.degraded[(decision["provider_id"], decision["model"])] = now + timedelta(
            minutes=5
        )
        LOG.warning("Could not persist routing state")


def safe_header(value):
    return "".join(c for c in str(value) if 32 <= ord(c) < 127)[:200]


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await execute_chat(request)


async def execute_chat(request: Request, *, candidate=None, request_id=None) -> Response:
    if not bearer_authorized(request):
        return api_error(401, "Invalid local gateway key", "unauthorized")
    raw_buffer = bytearray()
    async for chunk in request.stream():
        raw_buffer.extend(chunk)
        if len(raw_buffer) > MAX_BODY_BYTES:
            return api_error(413, "Request body is too large", "request_too_large")
    try:
        payload = json.loads(raw_buffer)
    except (ValueError, UnicodeDecodeError):
        return api_error(400, "Expected a JSON request body", "invalid_request")
    alias = payload.get("model") if isinstance(payload, dict) else None
    if (
        not isinstance(alias, str)
        or not re.fullmatch(r"qm/[a-z0-9]+(?:-[a-z0-9]+)*", alias)
        or len(alias) > 63
    ):
        return api_error(400, "Use model qm/<profile-slug>", "invalid_model")
    slug = alias[3:]
    if not isinstance(payload.get("stream", False), bool):
        return api_error(400, "stream must be a boolean", "invalid_request")
    decisions, profile, credentials = decision_snapshot(request.app, slug)
    if not profile:
        return api_error(
            503 if slug == "default" else 404,
            "Configure default first" if slug == "default" else "Profile not found",
            "not_configured" if slug == "default" else "profile_not_found",
        )
    profile_id, max_attempts = profile["id"], profile["max_attempts"]
    if candidate is not None:
        # Internal Doctor constraint, never accepted from public request JSON.
        max_attempts = 1
        decisions = [d for d in decisions if (d["position"], d["credential_id"]) == candidate]
    # Preserve the Phase 1 explicit paid-disabled error for a single paid connection.
    if decisions and all(d["skip_reason"] == "paid_blocked" for d in decisions):
        return api_error(403, "Paid or unknown-plan use is disabled", "paid_blocked")
    request_id = request_id or uuid.uuid4().hex
    streamed = payload.get("stream", False)
    deadline = time.monotonic() + (
        profile["first_event_timeout_s"] if streamed else profile["nonstream_deadline_s"]
    )
    # Local accounting needs stream usage. Ask for it only when the client did not, and only
    # from providers whose registry entry verified that stream_options.include_usage works.
    client_options = payload.get("stream_options")
    can_inject = streamed and (
        client_options is None
        or (isinstance(client_options, dict) and client_options.get("include_usage") is not True)
    )
    attempted = set()
    attempted_secrets = set()
    context_skip = set()
    count = 0
    last_response = None
    last_attempt = (None, None, {})  # error class, HTTP status and headers of the last attempt
    while count < max_attempts:
        # Recompute after every state change; never use a stale initial pool for fallback.
        decisions, profile, credentials = decision_snapshot(request.app, slug)
        if not profile or profile["id"] != profile_id or not profile["enabled"]:
            break
        selected = next(
            (
                d
                for d in decisions
                if d["eligible"]
                and (candidate is None or (d["position"], d["credential_id"]) == candidate)
                and (d["credential_id"], d["model"]) not in attempted
                and d["position"] not in context_skip
            ),
            None,
        )
        if selected is None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return deadline_response(count, *last_attempt)
        connection = credentials[selected["credential_id"]]
        selected["credential_revision"] = connection["fingerprint"]
        selected["credential_base_url"] = connection["base_url"]
        secret = connection["secret_value"] or os.environ.get(connection["env_name"] or "", "")
        identity = (
            connection["base_url"],
            selected["provider_id"],
            selected["model"],
            hashlib.sha256(secret.encode()).digest(),
        )
        if identity in attempted_secrets:
            attempted.add((selected["credential_id"], selected["model"]))
            continue
        attempted_secrets.add(identity)
        count += 1
        attempted.add((selected["credential_id"], selected["model"]))
        started = time.monotonic()
        details = {
            "ts": utc_now(),
            "request_id": request_id,
            "profile_id": profile_id,
            "profile_slug": slug,
            "plan_type": selected["plan_type"],
            "credential_label": selected["label"],
            # User-controlled labels can accidentally contain secrets; do not persist them.
            "client_label": None,
            "attempt_idx": count,
            "provider_id": selected["provider_id"],
            "model": selected["model"],
            "credential_id": selected["credential_id"],
            "streamed": int(streamed),
            "paid": int(selected["plan_type"] in {"PAID", "UNKNOWN"}),
            "skipped_json": json.dumps([d for d in decisions if not d["eligible"]]),
        }
        qm_headers = {
            "X-QuotaMesh-Profile": slug,
            "X-QuotaMesh-Provider": selected["provider_id"],
            "X-QuotaMesh-Model": safe_header(selected["model"]),
            "X-QuotaMesh-Key": safe_header(selected["label"]),
            "X-QuotaMesh-Attempts": str(count),
            "X-QuotaMesh-Fallback": str(count > 1).lower(),
            "X-QuotaMesh-Request-Id": request_id,
        }
        forwarded = dict(payload, model=selected["model"])
        provider = registry().get(selected["provider_id"])
        inject = can_inject and provider is not None and provider.stream_usage == "inject"
        if inject:
            forwarded["stream_options"] = {**(client_options or {}), "include_usage": True}
            qm_headers["X-QuotaMesh-Usage-Injected"] = "true"
        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if streamed else "application/json",
            "Accept-Encoding": "identity",
        }
        upstream = None
        body = b""
        status = 502
        media_type = "application/json"
        outcome = None
        metadata = {}
        first = remainder = b""
        chunks = None
        try:
            async with asyncio.timeout(remaining):
                client = request.app.state.client
                upstream_request = client.build_request(
                    "POST",
                    connection["base_url"].rstrip("/") + "/chat/completions",
                    content=json.dumps(
                        forwarded, separators=(",", ":"), ensure_ascii=False
                    ).encode(),
                    headers=headers,
                )
                upstream = await client.send(upstream_request, stream=True)
                details["ttfb_ms"] = int((time.monotonic() - started) * 1000)
                try:
                    request.app.state.store.save_rates(
                        selected,
                        rate_observations(
                            selected["provider_id"], upstream.headers, datetime.now(UTC)
                        ),
                    )
                except sqlite3.Error:
                    request.app.state.diagnostic_write_failures += 1
                    LOG.warning("Could not persist passive rate observations")
                status = upstream.status_code
                media_type = upstream.headers.get("content-type", "application/json").split(";")[0]
                if 300 <= status < 400:
                    outcome = Outcome("redirect", True, "target", "DEGRADED")
                    status = 502
                elif status >= 400 or not streamed:
                    body = await upstream.aread()
                    outcome = classify(
                        selected["provider_id"], status, upstream.headers, body, datetime.now(UTC)
                    )
                    if status < 400:
                        try:
                            if not isinstance(json.loads(body), dict):
                                raise TypeError("Expected an object")
                        except (ValueError, TypeError, UnicodeDecodeError):
                            outcome = Outcome("protocol", True, "target", "DEGRADED")
                            status = 502
                            body = b""
                    if outcome.kind == "ok":
                        metadata = usage_metadata(body, selected)
                elif media_type != "text/event-stream":
                    outcome = Outcome("protocol", True, "target", "DEGRADED")
                    status = 502
                else:
                    chunks = upstream.aiter_bytes()
                    # SSE comments/keepalives do not commit a model response.
                    preface = b""
                    pending = b""
                    while True:

                        async def pending_chunks(pending=pending, chunks=chunks):
                            if pending:
                                yield pending
                            async for chunk in chunks:
                                yield chunk

                        first, remainder = await first_sse_event(pending_chunks(), remaining)
                        event = sse_payload(first)
                        if event is None:
                            preface += first
                            if len(preface) > MAX_FIRST_EVENT_BYTES:
                                raise ValueError("Too many SSE keepalives before response")
                            pending = remainder
                            continue
                        if "error" in event or first_event_is_error(first):
                            # Google-style events name a status string; derive an HTTP status.
                            outcome = classify(
                                selected["provider_id"],
                                error_status(event.get("error")),
                                upstream.headers,
                                json.dumps(event).encode(),
                                datetime.now(UTC),
                            )
                            status = 502
                        else:
                            first = preface + first
                            details["ttfb_ms"] = int((time.monotonic() - started) * 1000)
                            outcome = Outcome("ok", False)
                        break
        except asyncio.CancelledError:
            if upstream:
                await upstream.aclose()
            _record_attempt(
                request,
                **details,
                outcome="CLIENT_CANCELLED",
                http_status=None,
                error_class="cancelled",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        except httpx.HTTPError:  # includes httpx timeouts: the upstream itself misbehaved
            outcome = Outcome("network", True, "target", "DEGRADED")
            status = 504 if time.monotonic() >= deadline else 502
        except TimeoutError:
            # The profile deadline ran out; that says nothing about the target, so keep it.
            outcome = Outcome("deadline", False)
            status = 504
        except (ValueError, UnicodeDecodeError):
            outcome = Outcome("protocol", True, "target", "DEGRADED")
            status = 502
        if streamed and outcome.kind == "ok" and status < 300:
            apply_outcome(request, selected, outcome)

            return StreamingResponse(
                relay_stream(
                    request,
                    first=first,
                    remainder=remainder,
                    chunks=chunks,
                    upstream=upstream,
                    selected=selected,
                    details=details,
                    started=started,
                    framed=inject,
                    idle_s=max(MIN_IDLE_TIMEOUT_S, profile["first_event_timeout_s"]),
                ),
                media_type="text/event-stream",
                headers=qm_headers,
            )
        if upstream:
            await upstream.aclose()
        apply_outcome(request, selected, outcome)
        label = (
            "OK"
            if outcome.kind == "ok"
            else "PRECOMMIT_ERROR"
            if streamed and status == 502
            else "UPSTREAM_ERROR"
        )
        # Known rejected requests carry zero charge; uncertain transport failures remain unknown.
        if (
            status >= 400
            and outcome.kind not in {"network", "protocol", "deadline"}
            or (
                outcome.kind == "ok"
                and selected["plan_type"] == "FREE"
                and metadata.get("provider_cost_usd") is None
            )
        ):
            metadata["estimated_cost_usd"] = 0
        _record_attempt(
            request,
            **details,
            **metadata,
            outcome=label,
            http_status=status,
            error_class=None if outcome.kind == "ok" else outcome.kind,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        last_attempt = (None if outcome.kind == "ok" else outcome.kind, status, qm_headers)
        if outcome.kind == "deadline":
            return deadline_response(count, *last_attempt)
        last_response = (
            Response(body, status_code=status, media_type=media_type, headers=qm_headers)
            if body and status != 502
            else api_error(status, "Upstream attempt failed before commitment", outcome.kind)
        )
        last_response.headers.update(qm_headers)
        if not outcome.retry:
            return last_response
        if outcome.scope == "next_target":
            context_skip.add(selected["position"])
    if time.monotonic() >= deadline:
        return deadline_response(count, *last_attempt)
    if last_response:
        return last_response
    return blocked_response(decisions, slug=slug, request_id=request_id)


def model_item(slug: str) -> dict[str, Any]:
    return {
        "id": "qm/" + slug,
        "object": "model",
        "created": MODELS_CREATED,
        "owned_by": "quotamesh",
    }


@router.get("/v1/models")
def models(request: Request) -> Response:
    if not bearer_authorized(request):
        return api_error(401, "Invalid local gateway key", "unauthorized")
    return JSONResponse(
        {
            "object": "list",
            "data": [
                model_item(p["slug"]) for p in request.app.state.store.profiles() if p["enabled"]
            ],
        }
    )


@router.get("/v1/models/{model_id:path}")
def model(request: Request, model_id: str) -> Response:
    if not bearer_authorized(request):
        return api_error(401, "Invalid local gateway key", "unauthorized")
    for p in request.app.state.store.profiles():
        if p["enabled"] and "qm/" + p["slug"] == model_id:
            return JSONResponse(model_item(p["slug"]))
    return api_error(
        404, "Model not found; GET /v1/models lists the callable qm/ aliases", "model_not_found"
    )
