"""Dashboard login links for the CLI; no secrets beyond a short-lived single-use token."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from quotamesh.security import (
    SESSION_LINK_TTL,
    api_error,
    bearer_authorized,
    mint_bootstrap_token,
    request_port,
)

router = APIRouter()


@router.post("/api/session/link")
def session_link(request: Request):
    """Mint a one-time dashboard link (bearer key only; a browser session cannot mint one)."""
    if not bearer_authorized(request):
        return api_error(401, "The local gateway key is required", "unauthorized")
    token = mint_bootstrap_token(request.app)
    port = request_port(request) or (request.scope.get("server") or (None, None))[1]
    origin = "http://127.0.0.1" + (f":{port}" if port else "")
    return JSONResponse(
        {"url": f"{origin}/bootstrap?token={token}", "expires_in": SESSION_LINK_TTL},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )
