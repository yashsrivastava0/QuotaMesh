"""Application factory and process lifecycle for the local QuotaMesh service."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from quotamesh.config import data_directory
from quotamesh.routes.dashboard import router as dashboard_router
from quotamesh.routes.gateway import router as gateway_router
from quotamesh.routes.wallet import router as wallet_router
from quotamesh.security import local_host_guard
from quotamesh.store import Store

UI = Path(__file__).parent / "ui"


def create_app(
    data_dir: Path | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    *,
    allow_test_host: bool = False,
) -> FastAPI:
    """Create the single-process local dashboard and OpenAI-compatible gateway."""
    store = Store(data_dir or data_directory())
    store.prune_history(datetime.now(UTC))
    client = httpx.AsyncClient(
        transport=upstream_transport,
        follow_redirects=False,
        timeout=httpx.Timeout(connect=5, read=60, write=10, pool=5),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(
        title="QuotaMesh", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.store = store
    app.state.local_key = store.gateway_key()
    app.state.bootstrap_token = secrets.token_urlsafe(32)
    app.state.session_token = secrets.token_urlsafe(32)
    app.state.client = client
    app.state.diagnostic_write_failures = 0
    app.state.degraded = {}
    app.state.accounting_failed = store.accounting_marker.exists()
    app.middleware("http")(local_host_guard(allow_test_host=allow_test_host))
    app.mount("/static", StaticFiles(directory=str(UI / "static")), name="static")
    app.include_router(dashboard_router)
    app.include_router(gateway_router)
    app.include_router(wallet_router)
    return app
