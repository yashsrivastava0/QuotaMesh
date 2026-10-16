"""Application factory and process lifecycle for the local QuotaMesh service."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from quotamesh.activity import ActivityFeed
from quotamesh.config import data_directory
from quotamesh.lifecycle import process_lock
from quotamesh.routes.connect import router as connect_router
from quotamesh.routes.dashboard import router as dashboard_router
from quotamesh.routes.gateway import router as gateway_router
from quotamesh.routes.session import router as session_router
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
        # Reads are bounded by profile deadlines and the gateway's post-commit idle timeout.
        timeout=httpx.Timeout(connect=5, read=None, write=10, pool=5),
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            if getattr(application.state, "lock_held", False):
                # `quotamesh start` already holds this data directory's lock.
                yield
            else:
                with process_lock(store.data_dir):
                    yield
        finally:
            try:
                await client.aclose()
            finally:
                store.close()

    app = FastAPI(
        title="QuotaMesh", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.store = store
    app.state.local_key = store.gateway_key()
    # The printed one-time link never expires until used; CLI/browser links expire (security.py).
    app.state.bootstrap_token = secrets.token_urlsafe(32)
    app.state.bootstrap_tokens = {app.state.bootstrap_token: None}
    app.state.session_token = secrets.token_urlsafe(32)
    app.state.client = client
    app.state.lock_held = False
    app.state.diagnostic_write_failures = 0
    app.state.degraded = {}
    app.state.accounting_failed = store.accounting_marker.exists()
    app.state.activity = ActivityFeed()
    app.state.doctor_running = set()
    app.middleware("http")(local_host_guard(allow_test_host=allow_test_host))
    app.mount("/static", StaticFiles(directory=str(UI / "static")), name="static")
    app.include_router(dashboard_router)
    app.include_router(gateway_router)
    app.include_router(wallet_router)
    app.include_router(connect_router)
    app.include_router(session_router)
    return app
