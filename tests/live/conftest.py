"""Opt-in live suite: real OpenAI and Gemini behind an in-process QuotaMesh.

    python -m pytest -m live tests/live -q --tb=short -p no:cacheprovider

Keys come from the checkout's ``.env`` (``QM_ENV_FILE`` overrides; a linked git worktree
also finds its main checkout's file) through ``quotamesh.envfile``. They are placed in
this process's environment as ``QM_LIVE_OPENAI_KEY`` / ``QM_LIVE_GEMINI_KEY`` for the
session and saved in QuotaMesh only as environment references, so the temporary
databases never contain key values. Budgets, model overrides and safety rules are
described in docs/live-testing.md. Nothing in this suite prints key values, prompts or
completions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from quotamesh.app import create_app
from quotamesh.envfile import (
    EnvEntry,
    EnvFileError,
    Secret,
    is_placeholder,
    parse_env,
    provider_for,
)

from .helpers import (
    GEMINI_BASE,
    OPENAI_BASE,
    CountingTransport,
    LiveBudget,
    LiveGateway,
    ProviderHealth,
    choose_gemini_model,
    choose_openai_model,
    find_env_file,
    list_models,
    live_data_dir,
)

ENV_NAMES = {"openai": "QM_LIVE_OPENAI_KEY", "gemini": "QM_LIVE_GEMINI_KEY"}
_BUDGET: LiveBudget | None = None


@dataclass
class LiveEnvironment:
    path: Path | None
    problem: str | None = None
    entries: dict[str, EnvEntry] = field(default_factory=dict)
    secrets: dict[str, Secret] = field(default_factory=dict)

    def setting(self, name: str, default: str | None = None) -> str | None:
        """A non-secret override from the process environment, then from the .env file."""
        if os.environ.get(name):
            return os.environ[name]
        entry = self.entries.get(name)
        if entry is not None and not is_placeholder(entry.value.reveal()):
            return entry.value.reveal().strip()
        return default

    def values(self) -> list[str]:
        """Key values, for leak checks only. Never print or assert on these directly."""
        return [secret.reveal().strip() for secret in self.secrets.values()]


@pytest.fixture(scope="session")
def live_environment():
    path = find_env_file(Path(__file__).resolve().parent)
    environment = LiveEnvironment(path)
    if path is None:
        environment.problem = "no .env file found (set QM_ENV_FILE to its path)"
    elif not path.is_file():
        environment.problem = "QM_ENV_FILE does not point to a file"
    else:
        try:
            environment.entries = parse_env(path.read_bytes()).entries
        except (OSError, EnvFileError) as exc:  # messages never include file content
            environment.problem = f"the .env file is unreadable: {exc}"
    for entry in sorted(environment.entries.values(), key=lambda entry: entry.line):
        provider = provider_for(entry.name)
        if (
            provider in ENV_NAMES
            and provider not in environment.secrets
            and not is_placeholder(entry.value.reveal())
        ):
            environment.secrets[provider] = entry.value
    with pytest.MonkeyPatch.context() as patch:
        for provider, secret in environment.secrets.items():
            patch.setenv(ENV_NAMES[provider], secret.reveal().strip())
        yield environment


def _key_reference(environment: LiveEnvironment, provider: str) -> str:
    if environment.problem:
        pytest.skip(f"live keys unavailable: {environment.problem}")
    if provider not in environment.secrets:
        pytest.skip(f"no usable {provider} key in {environment.path.name} (missing or placeholder)")
    return ENV_NAMES[provider]


@pytest.fixture(scope="session")
def openai_key(live_environment) -> str:
    """The environment variable name that holds the OpenAI key (never the value)."""
    return _key_reference(live_environment, "openai")


@pytest.fixture(scope="session")
def gemini_key(live_environment) -> str:
    """The environment variable name that holds the Gemini key (never the value)."""
    return _key_reference(live_environment, "gemini")


@pytest.fixture(scope="session")
def live_budget(live_environment) -> LiveBudget:
    global _BUDGET
    _BUDGET = LiveBudget(
        {
            "openai": int(live_environment.setting("QM_LIVE_MAX_OPENAI", "40")),
            "gemini": int(live_environment.setting("QM_LIVE_MAX_GEMINI", "30")),
        }
    )
    return _BUDGET


@pytest.fixture(scope="session")
def provider_health() -> ProviderHealth:
    return ProviderHealth()


@pytest.fixture(scope="session")
def openai_model(live_environment, openai_key) -> str:
    """QM_LIVE_OPENAI_MODEL, else gpt-4.1-nano, gpt-4o-mini or gpt-5-nano from a free listing."""
    if override := live_environment.setting("QM_LIVE_OPENAI_MODEL"):
        return override
    model = choose_openai_model(list_models(OPENAI_BASE, os.environ[openai_key], "OpenAI"))
    if model is None:
        pytest.skip("no inexpensive OpenAI chat model (gpt-4.1-nano, gpt-4o-mini, gpt-5-nano)")
    return model


@pytest.fixture(scope="session")
def gemini_model(live_environment, gemini_key) -> str:
    """QM_LIVE_GEMINI_MODEL, else the newest non-preview flash-lite (then flash) listed."""
    if override := live_environment.setting("QM_LIVE_GEMINI_MODEL"):
        return override.removeprefix("models/")
    model = choose_gemini_model(list_models(GEMINI_BASE, os.environ[gemini_key], "Gemini"))
    if model is None:
        pytest.skip("no Gemini flash-lite or flash model is listed for this key")
    return model


@pytest.fixture
async def gateway(live_budget):
    """A fresh in-process QuotaMesh with its own temporary data directory per test."""
    with live_data_dir() as directory:
        transport = CountingTransport(httpx.AsyncHTTPTransport(), live_budget)
        app = create_app(directory, transport, allow_test_host=True)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=300
        )
        try:
            yield LiveGateway(app, client, transport, directory)
        finally:
            await client.aclose()
            await app.state.client.aclose()


def pytest_terminal_summary(terminalreporter):
    if _BUDGET is not None:
        terminalreporter.write_line("QuotaMesh live budget used: " + _BUDGET.summary())
