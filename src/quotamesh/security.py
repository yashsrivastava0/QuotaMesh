"""Local-only request checks and dashboard/API authorization helpers."""

from __future__ import annotations

import secrets
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


def browser_authorized(request: Request) -> bool:
    cookie = request.cookies.get("qm_session", "")
    return bool(cookie) and secrets.compare_digest(cookie, request.app.state.session_token)


def bearer_authorized(request: Request) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(value, request.app.state.local_key)


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
