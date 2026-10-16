"""Budget, privacy and protocol helpers for the opt-in live suite.

Nothing here logs or returns request bodies, response bodies, headers or key material.
Failure messages carry only statuses, error codes and short descriptions.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

OPENAI_BASE = "https://api.openai.com/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
PROVIDER_HOSTS = {"api.openai.com": "openai", "generativelanguage.googleapis.com": "gemini"}
# Largest token cap the suite may send; every chat request must carry one.
TOKEN_LIMITS = {"openai": 64, "gemini": 256}
GEMINI_SPACING_S = 5.0
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
# Error codes QuotaMesh itself returns; they are never provider capacity evidence.
LOCAL_CODES = {
    "unauthorized",
    "invalid_model",
    "profile_not_found",
    "not_configured",
    "paid_blocked",
    "no_candidates",
    "deadline_exceeded",
    "request_too_large",
}
PROMPT_TEXT = "Reply with the single word OK."
PROMPT = [{"role": "user", "content": PROMPT_TEXT}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": "Look up an Acme order by its id.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    }
]


def lookup_order(order_id: str) -> dict[str, str]:
    """The local function the model may call."""
    return {"order_id": str(order_id), "status": "shipped", "carrier": "Acme Express"}


class BudgetExceeded(RuntimeError):
    """The suite tried to exceed its call budget or send an uncapped generation request.

    Deliberately not an OSError or httpx error, so gateway code can never mistake it for a
    retryable network failure and fall back to another real provider.
    """


class LiveBudget:
    """Session-wide chat-call budget shared by every app the suite creates."""

    def __init__(self, limits: dict[str, int]):
        self.limits = dict(limits)
        self.posts = dict.fromkeys(limits, 0)
        self.listings = dict.fromkeys(limits, 0)
        self.statuses: dict[str, dict[int, int]] = {provider: {} for provider in limits}
        self._next_gemini = 0.0
        self._lock = threading.Lock()

    def reserve(self, provider: str) -> float:
        """Count one chat POST and return how long to wait before sending it."""
        with self._lock:
            if self.posts[provider] >= self.limits[provider]:
                raise BudgetExceeded(
                    f"{provider} live budget of {self.limits[provider]} chat calls is spent"
                )
            self.posts[provider] += 1
            if provider != "gemini":
                return 0.0
            now = time.monotonic()
            slot = max(now, self._next_gemini)
            self._next_gemini = slot + GEMINI_SPACING_S
            return slot - now

    def record(self, provider: str, status: int) -> None:
        with self._lock:
            self.statuses[provider][status] = self.statuses[provider].get(status, 0) + 1

    def listing(self, provider: str) -> None:
        with self._lock:
            self.listings[provider] += 1

    def count(self, provider: str) -> int:
        with self._lock:
            return self.posts[provider]

    def summary(self) -> str:
        with self._lock:
            parts = []
            for provider, limit in self.limits.items():
                statuses = ", ".join(
                    f"{status}x{count}" for status, count in sorted(self.statuses[provider].items())
                )
                parts.append(
                    f"{provider}: {self.posts[provider]}/{limit} chat calls"
                    f" [{statuses or 'none'}], {self.listings[provider]} model listings"
                )
            return "; ".join(parts)


def check_token_cap(provider: str, body: bytes) -> None:
    """Refuse a chat request that does not cap its output tokens."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise BudgetExceeded("chat request body is not JSON") from None
    limit = TOKEN_LIMITS[provider]
    caps = [payload[name] for name in ("max_completion_tokens", "max_tokens") if name in payload]
    if not caps or not all(
        isinstance(cap, int) and not isinstance(cap, bool) and 0 < cap <= limit for cap in caps
    ):
        raise BudgetExceeded(
            f"{provider} chat requests must set max_completion_tokens or max_tokens <= {limit}"
        )


class CountingTransport(httpx.AsyncBaseTransport):
    """Upstream transport for live apps: budgets, caps and paces real provider calls."""

    def __init__(self, inner: httpx.AsyncBaseTransport, budget: LiveBudget):
        self.inner = inner
        self.budget = budget
        self.posts = 0  # chat calls made by this app, for zero-upstream-call assertions

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        provider = PROVIDER_HOSTS.get(request.url.host or "")
        if provider is None:
            raise BudgetExceeded("the live suite only contacts OpenAI and Gemini")
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            check_token_cap(provider, request.content)
            wait = self.budget.reserve(provider)
            self.posts += 1
            if wait > 0:
                await asyncio.sleep(wait)
            response = await self.inner.handle_async_request(request)
            self.budget.record(provider, response.status_code)
            return response
        if request.method == "GET" and request.url.path.endswith("/models"):
            self.budget.listing(provider)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class ProviderHealth:
    """Remembers providers that ran out of capacity so later tests skip instead of spending."""

    def __init__(self) -> None:
        self.reasons: dict[str, str] = {}

    def require(self, provider: str) -> None:
        if reason := self.reasons.get(provider):
            pytest.skip(reason)

    def disable(self, provider: str, reason: str) -> None:
        self.reasons[provider] = reason
        pytest.skip(reason)


class Checks:
    """Soft assertions: one expensive live run reports every unmet expectation at once."""

    def __init__(self) -> None:
        self.failed: list[str] = []

    def __call__(self, condition: Any, description: str) -> bool:
        if not condition:
            self.failed.append(description)
        return bool(condition)

    def verify(self) -> None:
        assert not self.failed, "Unmet expectations:\n- " + "\n- ".join(self.failed)


@dataclass
class ServerSentEvents:
    chunks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    done: bool = False

    @property
    def text(self) -> str:
        return "".join(
            (choice.get("delta") or {}).get("content") or ""
            for chunk in self.chunks
            for choice in chunk.get("choices") or []
        )


def parse_sse(text: str) -> ServerSentEvents:
    result = ServerSentEvents()
    for block in re.split(r"\r?\n\r?\n", text):
        lines = block.splitlines()
        data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        event = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
        if not data:
            continue
        if data == "[DONE]":
            result.done = True
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            result.errors.append("malformed event")
            continue
        if event == "error" or (isinstance(payload, dict) and "error" in payload):
            error = payload.get("error") if isinstance(payload, dict) else None
            result.errors.append(str((error or {}).get("type", "error")))
            continue
        if isinstance(payload, dict):
            result.chunks.append(payload)
    return result


def error_code(response: httpx.Response) -> str | None:
    """Return an error code from an OpenAI-shaped object or Gemini's root-array body."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0]
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code") or error.get("status") or error.get("type")
    return None if code is None else str(code)


def contains_any(text: str, values: list[str]) -> bool:
    return any(value and value in text for value in values)


def files_contain(directory: Path, values: list[str]) -> bool:
    """True when any file below directory contains one of the values (bytes compare)."""
    needles = [value.encode() for value in values if len(value) >= 8]
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(needle in data for needle in needles):
            return True
    return False


def openai_extra(model: str) -> dict[str, str]:
    """GPT-5 models spend output tokens on reasoning unless asked not to."""
    return {"reasoning_effort": "minimal"} if model.startswith("gpt-5") else {}


def choose_openai_model(ids: list[str]) -> str | None:
    return next((m for m in ("gpt-4.1-nano", "gpt-4o-mini", "gpt-5-nano") if m in ids), None)


GEMINI_SKIP = re.compile(
    r"image|tts|live|audio|embedding|veo|imagen|exp|robotics|computer|thinking"
)


def _gemini_version(name: str) -> float:
    match = re.match(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(match.group(1)) if match else 0.0


def choose_gemini_model(ids: list[str]) -> str | None:
    """Newest non-preview flash-lite, then flash; preview builds only as a last resort."""
    bare = sorted({identifier.removeprefix("models/") for identifier in ids})
    for preview in (False, True):
        for family in ("flash-lite", "flash"):
            pool = [
                name
                for name in bare
                if name.startswith("gemini-")
                and family in name
                and (family != "flash" or "flash-lite" not in name)
                and not GEMINI_SKIP.search(name)
                and ("preview" in name) == preview
            ]
            if pool:
                return max(pool, key=lambda name: (_gemini_version(name), -len(name)))
    return None


def list_models(base_url: str, secret: str, provider: str) -> list[str]:
    """One free GET /models directly to the provider, for model discovery."""
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        response = client.get(base_url + "/models", headers={"Authorization": "Bearer " + secret})
    if response.status_code != 200:
        pytest.skip(f"{provider} model listing failed with HTTP {response.status_code}")
    data = response.json().get("data") or []
    return [m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]


def is_quotamesh_checkout(directory: Path) -> bool:
    try:
        return 'name = "quotamesh"' in (directory / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False


def worktree_main_root(directory: Path) -> Path | None:
    """For a linked git worktree, return the main checkout that owns it."""
    marker = directory / ".git"
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip())
    if not gitdir.is_absolute():
        gitdir = (directory / gitdir).resolve()
    # <main checkout>/.git/worktrees/<name>
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return None


def find_env_file(start: Path) -> Path | None:
    """QM_ENV_FILE, else the .env of the enclosing QuotaMesh checkout or its main worktree."""
    override = os.environ.get("QM_ENV_FILE")
    if override:
        return Path(override).expanduser()
    for directory in (start, *start.parents):
        if not is_quotamesh_checkout(directory):
            continue
        if (directory / ".env").is_file():
            return directory / ".env"
        main = worktree_main_root(directory)
        if main is not None and (main / ".env").is_file():
            return main / ".env"
    return None


def close_store(app) -> None:
    """Release the app's SQLite handles.

    Stores that reuse connections close them in the app lifespan, which httpx.ASGITransport
    never runs; on Windows open database files cannot be deleted.
    """
    close = getattr(app.state.store, "close", None)
    if callable(close):
        close()


def remove_data_dir(path: Path) -> None:
    """Delete a temporary data directory, retrying while Windows releases file handles."""
    for _ in range(25):
        gc.collect()  # unreachable connections release their files when collected
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.2)
    warnings.warn(f"could not delete live data directory {path}; delete it manually", stacklevel=2)


@contextmanager
def live_data_dir() -> Iterator[Path]:
    """A temporary QuotaMesh data directory that is deleted afterwards (or reported)."""
    path = Path(tempfile.mkdtemp(prefix="quotamesh-live-"))
    try:
        yield path
    finally:
        remove_data_dir(path)


@contextmanager
def serve(app) -> Iterator[str]:
    """Run an app on a free loopback port in a background Uvicorn thread."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "the local QuotaMesh server did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


class LiveGateway:
    """An in-process QuotaMesh driven through httpx.ASGITransport."""

    def __init__(self, app, client: httpx.AsyncClient, transport: CountingTransport, data_dir):
        self.app = app
        self.client = client
        self.transport = transport
        self.budget = transport.budget
        self.data_dir = Path(data_dir)
        self.auth = {"Authorization": "Bearer " + app.state.local_key}

    async def api(self, method: str, path: str, *, json=None, expected=(200, 201)) -> Any:
        response = await self.client.request(method, path, json=json, headers=self.auth)
        assert response.status_code in expected, (
            f"{method} {path} returned HTTP {response.status_code} ({error_code(response)})"
        )
        return response.json()

    async def add_reference(
        self, provider_id: str, env_name: str, plan_type: str, **fields: Any
    ) -> int:
        """Save a credential as an environment reference; the key value is never stored."""
        body = {"provider_id": provider_id, "plan_type": plan_type, "env_name": env_name}
        body["label"] = fields.pop("label", f"{provider_id} live key")
        body.update(fields)
        return (await self.api("POST", "/api/credentials", json=body, expected=(201,)))[
            "credential_id"
        ]

    @staticmethod
    def profile_body(slug: str, targets: list[dict[str, Any]], **policy: Any) -> dict[str, Any]:
        return {
            "slug": slug,
            "name": policy.pop("name", slug.replace("-", " ").title()),
            "targets": targets,
            "allow_paid": False,
            "allow_trial": True,
            "allow_unknown_price": False,
            "enabled": True,
            "max_attempts": 5,
            "nonstream_deadline_s": 90,
            "first_event_timeout_s": 45,
            "paid_daily_cap_usd": None,
            "paid_monthly_cap_usd": None,
            **policy,
        }

    async def create_profile(self, slug: str, targets: list[dict[str, Any]], **policy) -> None:
        body = self.profile_body(slug, targets, **policy)
        await self.api("POST", "/api/profiles", json=body, expected=(201,))

    async def update_profile(self, slug: str, targets: list[dict[str, Any]], **policy) -> None:
        body = self.profile_body(slug, targets, **policy)
        await self.api("PUT", f"/api/profiles/{slug}", json=body, expected=(200,))

    async def setup_default(self, credential_id: int, model: str) -> None:
        body = {"credential_id": credential_id, "model": model}
        await self.api("POST", "/api/setup", json=body, expected=(201,))

    async def chat(self, payload: dict[str, Any]) -> httpx.Response:
        return await self.client.post("/v1/chat/completions", json=payload, headers=self.auth)

    async def trace(self, response: httpx.Response, checks: Checks) -> dict[str, Any]:
        """GET /api/requests/{id} for a gateway response; {} (and a soft failure) if absent."""
        request_id = response.headers.get("x-quotamesh-request-id", "")
        found = await self.client.get(f"/api/requests/{request_id}", headers=self.auth)
        if not checks(found.status_code == 200, "GET /api/requests/{id} returns the trace"):
            return {}
        return found.json()

    async def ensure_served(
        self, response: httpx.Response, provider: str, health: ProviderHealth
    ) -> None:
        """Pass on success; skip the provider on capacity exhaustion; fail otherwise."""
        if response.status_code < 400:
            return
        code = error_code(response)
        error_class = None
        request_id = response.headers.get("x-quotamesh-request-id")
        if request_id:
            found = await self.client.get(f"/api/requests/{request_id}", headers=self.auth)
            if found.status_code == 200:
                error_class = ((found.json().get("attempts") or [{}])[-1]).get("error_class")
        status = response.status_code
        if provider == "openai" and code == "insufficient_quota":
            assert error_class in (None, "funds_or_account"), (
                f"insufficient_quota was classified as {error_class}"
            )
            health.disable(
                "openai",
                "OpenAI reported insufficient_quota (billing or credit); "
                "remaining OpenAI generation tests are skipped",
            )
        if provider == "gemini" and status in (429, 503) and code not in LOCAL_CODES:
            expected = {"rate_limit", "daily_quota"} if status == 429 else {"provider_failure"}
            assert error_class is None or error_class in expected, (
                f"Gemini HTTP {status} was classified as {error_class}"
            )
            health.disable(
                "gemini",
                f"Gemini returned HTTP {status} (rate limit or high demand); "
                "remaining Gemini generation tests are skipped",
            )
        pytest.fail(f"{provider} request was not served: HTTP {status}, code {code}")


def check_route_headers(
    checks: Checks,
    response: httpx.Response,
    *,
    profile: str,
    provider: str,
    attempts: int,
    model: str | None = None,
) -> None:
    headers = response.headers
    checks(headers.get("x-quotamesh-profile") == profile, f"X-QuotaMesh-Profile is {profile}")
    checks(headers.get("x-quotamesh-provider") == provider, f"X-QuotaMesh-Provider is {provider}")
    if model is not None:
        checks(headers.get("x-quotamesh-model") == model, f"X-QuotaMesh-Model is {model}")
    checks(
        headers.get("x-quotamesh-attempts") == str(attempts), f"X-QuotaMesh-Attempts is {attempts}"
    )
    fallback = "true" if attempts > 1 else "false"
    checks(headers.get("x-quotamesh-fallback") == fallback, f"X-QuotaMesh-Fallback is {fallback}")
    checks(
        REQUEST_ID.fullmatch(headers.get("x-quotamesh-request-id", "")),
        "X-QuotaMesh-Request-Id is 32 lowercase hex",
    )


def expected_cost(usage: dict[str, Any], input_price: float, output_price: float) -> float:
    """Local estimate in USD; output is max(completion, total - prompt) per the usage rules."""
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    output = max(completion, (usage.get("total_tokens") or 0) - prompt)
    return (prompt * input_price + output * output_price) / 1e6
