"""Local-only request checks and dashboard/API authorization helpers."""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError


def configuration_error(exc: Exception) -> JSONResponse:
    """Expose field locations and fixed advice, never validation input/context."""
    fields = []
    message = "Check the highlighted fields, then save again."
    if isinstance(exc, ValidationError):
        for error in exc.errors(include_input=False, include_context=False):
            advice = {
                "missing": "This field is required.",
                "literal_error": "Choose one of the listed options.",
                "string_pattern_mismatch": "Use lowercase letters, numbers, and single hyphens.",
                "too_short": "Add at least one target.",
                "extra_forbidden": "This field is not supported.",
            }.get(error["type"], "Check this field’s format and permitted limits.")
            fields.append({"path": list(error["loc"]), "message": advice})
    else:
        text = str(exc)
        if "secret" in text.lower() or "reference" in text.lower():
            message = "Provide exactly one API key or environment reference when adding or rotating access."
            fields = [
                {"path": ["secret_value"], "message": message},
                {"path": ["env_name"], "message": message},
            ]
        elif "endpoint" in text.lower() or "https" in text.lower() or "url" in text.lower():
            message = "Use an HTTPS endpoint, or HTTP on loopback for a local provider."
            fields = [{"path": ["base_url"], "message": message}]
        elif "environment" in text.lower():
            fields = [{"path": ["env_name"], "message": "Enter a valid environment variable name."}]
        elif "Pinned credential" in text:
            message = "Each pinned key must belong to its target provider. Choose a matching key."
        else:
            message = (
                "Invalid configuration. Check the fields, target credentials, and permitted limits."
            )
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": "invalid_configuration",
                "code": "invalid_configuration",
                "fields": fields,
            }
        },
        status_code=400,
    )


def api_error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": code, "code": code}}, status_code=status
    )


SESSION_LINK_TTL = 120  # seconds a CLI/browser login link stays usable
MAX_PENDING_LINKS = 32  # expiring links kept at once; the printed link is never evicted


def secret_matches(supplied: str | None, expected: str | None) -> bool:
    """Constant-time comparison on UTF-8 bytes; any text (even non-ASCII) is a clean miss."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8", "replace"), expected.encode("utf-8"))


def request_port(request: Request) -> int | None:
    """The port the client addressed (from the validated loopback Host), if any."""
    try:
        return request.url.port
    except ValueError:
        return None


def session_cookie_name(request: Request) -> str:
    # Cookies ignore ports, so instances on different ports need distinct names.
    port = request_port(request)
    return f"qm_session_{port}" if port else "qm_session"


def browser_authorized(request: Request) -> bool:
    return secret_matches(
        request.cookies.get(session_cookie_name(request)), request.app.state.session_token
    )


def bearer_authorized(request: Request) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and secret_matches(value, request.app.state.local_key)


def mint_bootstrap_token(app, ttl: float | None = SESSION_LINK_TTL) -> str:
    """Create a single-use login token; ``ttl=None`` never expires (the printed link)."""
    tokens = app.state.bootstrap_tokens
    now = time.monotonic()
    for token, expiry in list(tokens.items()):
        if expiry is not None and expiry <= now:
            tokens.pop(token, None)
    pending = sorted((expiry, token) for token, expiry in tokens.items() if expiry is not None)
    while len(pending) >= MAX_PENDING_LINKS:
        tokens.pop(pending.pop(0)[1], None)
    token = secrets.token_urlsafe(32)
    tokens[token] = None if ttl is None else now + ttl
    return token


def consume_bootstrap_token(app, supplied: str | None) -> bool:
    """Use a login token once. Expired or unknown tokens fail; a matched token is spent."""
    state = app.state
    candidates = list(state.bootstrap_tokens.items())
    printed = getattr(state, "bootstrap_token", None)
    if printed and printed not in state.bootstrap_tokens:
        candidates.append((printed, None))  # Assigned directly (older callers and scripts).
    for token, expiry in candidates:
        if secret_matches(supplied, token):
            state.bootstrap_tokens.pop(token, None)
            if token == printed:
                state.bootstrap_token = None
            return expiry is None or expiry > time.monotonic()
    return False


def management_authorized(request: Request) -> bool:
    return browser_authorized(request) or bearer_authorized(request)


def local_host_guard(
    *, allow_test_host: bool = False
) -> Callable[[Request, Callable[..., Awaitable[Response]]], Awaitable[Response]]:
    """Reject foreign Host and cross-origin writes to the loopback service."""

    async def guard(request: Request, call_next: Callable[..., Awaitable[Response]]) -> Response:
        host = urlsplit("//" + request.headers.get("host", "")).hostname
        allowed = {"127.0.0.1", "localhost"}
        if allow_test_host:
            allowed.add("testserver")
        if host not in allowed:
            return api_error(403, "Invalid local Host header", "forbidden_host")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers['host']}":
                return api_error(403, "Foreign Origin is not allowed", "forbidden_origin")
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return api_error(403, "Cross-site requests are not allowed", "forbidden_origin")
        return await call_next(request)

    return guard
