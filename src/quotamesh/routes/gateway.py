"""OpenAI-compatible model gateway and upstream streaming transport."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from quotamesh.security import api_error, bearer_authorized
from quotamesh.store import utc_now

router = APIRouter()
LOG = logging.getLogger("quotamesh")
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_FIRST_EVENT_BYTES = 64 * 1024


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


def _usage(body: bytes) -> tuple[int | None, int | None]:
    try:
        usage = json.loads(body).get("usage", {})
        if not isinstance(usage, dict):
            return None, None
        return usage.get("prompt_tokens"), usage.get("completion_tokens")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None, None


def _record_attempt(request: Request, **values: Any) -> None:
    try:
        request.app.state.store.record_attempt(**values)
    except sqlite3.Error:
        request.app.state.diagnostic_write_failures += 1
        LOG.warning("Could not write request metadata")


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
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
    if not isinstance(payload, dict) or payload.get("model") != "qm/default":
        return api_error(400, "Use model qm/default", "invalid_model")
    if not isinstance(payload.get("stream", False), bool):
        return api_error(400, "stream must be a boolean", "invalid_request")

    store = request.app.state.store
    connection = store.runtime_connection()
    if not connection:
        return api_error(503, "Configure the default connection first", "not_configured")
    if not connection["enabled"] or not connection["profile_enabled"]:
        return api_error(503, "Default connection is disabled", "disabled")
    if connection["plan_type"] in {"PAID", "UNKNOWN"} and not connection["allow_paid"]:
        return api_error(403, "Paid or unknown-plan use is disabled for qm/default", "paid_blocked")
    secret = connection["secret_value"] or os.environ.get(connection["env_name"] or "", "")
    if not secret:
        return api_error(503, "The configured environment secret is unavailable", "missing_secret")

    payload["model"] = connection["model"]
    forwarded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    streamed = bool(payload.get("stream", False))
    details = {
        "ts": utc_now(),
        "request_id": request_id,
        "profile_id": connection["profile_id"],
        "client_label": request.headers.get("x-quotamesh-client", "")[:100],
        "attempt_idx": 1,
        "provider_id": connection["provider_id"],
        "model": connection["model"],
        "credential_id": connection["id"],
        "streamed": int(streamed),
    }
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if streamed else "application/json",
        "Accept-Encoding": "identity",
    }
    url = connection["base_url"].rstrip("/") + "/chat/completions"
    client: httpx.AsyncClient = request.app.state.client
    try:
        upstream_request = client.build_request("POST", url, content=forwarded, headers=headers)
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        _record_attempt(
            request,
            **details,
            outcome="NETWORK_ERROR",
            error_class="network",
            http_status=None,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return api_error(502, "Upstream connection failed", "upstream_connection")

    def safe_header(value: str) -> str:
        return value.encode("ascii", "ignore").decode("ascii")[:200]

    qm_headers = {
        "X-QuotaMesh-Profile": "default",
        "X-QuotaMesh-Provider": connection["provider_id"],
        "X-QuotaMesh-Model": safe_header(connection["model"]),
        "X-QuotaMesh-Key": safe_header(connection["label"]),
        "X-QuotaMesh-Attempts": "1",
        "X-QuotaMesh-Fallback": "false",
        "X-QuotaMesh-Request-Id": request_id,
    }
    media_type = upstream.headers.get("content-type", "application/json").split(";")[0]
    if 300 <= upstream.status_code < 400:
        await upstream.aclose()
        _record_attempt(
            request,
            **details,
            outcome="UPSTREAM_REDIRECT",
            error_class="redirect",
            http_status=502,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return api_error(502, "Upstream redirect blocked", "upstream_redirect")

    if not streamed or upstream.status_code >= 400:
        try:
            body = await upstream.aread()
        except httpx.HTTPError:
            await upstream.aclose()
            _record_attempt(
                request,
                **details,
                outcome="NETWORK_ERROR",
                error_class="network",
                http_status=None,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            return api_error(502, "Upstream response failed", "upstream_connection")
        finally:
            await upstream.aclose()
        input_tokens, output_tokens = (
            _usage(body) if upstream.status_code < 400 else (None, None)
        )
        _record_attempt(
            request,
            **details,
            outcome="OK" if upstream.status_code < 400 else "UPSTREAM_ERROR",
            http_status=upstream.status_code,
            error_class=None if upstream.status_code < 400 else "upstream",
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return Response(
            body,
            status_code=upstream.status_code,
            media_type=media_type,
            headers=qm_headers,
        )

    if media_type != "text/event-stream":
        await upstream.aclose()
        _record_attempt(
            request,
            **details,
            outcome="PROTOCOL_ERROR",
            error_class="protocol",
            http_status=502,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return api_error(502, "Expected an upstream SSE stream", "upstream_protocol")

    chunks = upstream.aiter_bytes()
    try:
        first, remainder = await first_sse_event(chunks, 30)
    except (TimeoutError, httpx.HTTPError, ValueError):
        await upstream.aclose()
        _record_attempt(
            request,
            **details,
            outcome="PRECOMMIT_ERROR",
            error_class="stream",
            http_status=502,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return api_error(502, "Upstream stream failed before its first event", "upstream_stream")
    if first_event_is_error(first):
        await upstream.aclose()
        _record_attempt(
            request,
            **details,
            outcome="PRECOMMIT_ERROR",
            error_class="upstream",
            http_status=502,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return api_error(
            502, "Upstream returned an error before streaming began", "upstream_stream"
        )

    async def body_stream() -> AsyncIterator[bytes]:
        outcome = "OK"
        try:
            yield first
            if remainder:
                yield remainder
            async for chunk in chunks:
                yield chunk
        except asyncio.CancelledError:
            outcome = "CLIENT_CANCELLED"
            raise
        except (httpx.HTTPError, TimeoutError):
            outcome = "MID_STREAM_FAILURE"
            yield b'event: error\ndata: {"error":{"message":"Upstream stream interrupted","type":"upstream_stream"}}\n\n'
        finally:
            await upstream.aclose()
            _record_attempt(
                request,
                **details,
                outcome=outcome,
                http_status=200,
                error_class=None if outcome == "OK" else "stream",
                latency_ms=int((time.monotonic() - started) * 1000),
            )

    return StreamingResponse(body_stream(), media_type="text/event-stream", headers=qm_headers)


@router.get("/v1/models")
def models(request: Request) -> Response:
    """Expose the single model alias accepted by the Phase 1 gateway."""
    if not bearer_authorized(request):
        return api_error(401, "Invalid local gateway key", "unauthorized")
    connection = request.app.state.store.connection_summary()
    models_list = (
        [{"id": "qm/default", "object": "model", "owned_by": "quotamesh"}] if connection else []
    )
    return JSONResponse({"object": "list", "data": models_list})
