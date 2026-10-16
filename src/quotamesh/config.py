"""Local configuration and dated provider registry."""

from __future__ import annotations

import functools
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

STREAM_USAGE_MODES = {"passthrough", "inject"}
RESET_RULES = {"", "midnight_pt"}


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    last_verified: str
    # Explicit, evidence-backed quirk flags (docs/live-provider-notes.md). The defaults are
    # plain OpenAI-compatible behaviour, so unverified presets change nothing.
    stream_usage: str = "passthrough"  # "inject": request stream usage for local accounting
    token_param: str = "max_tokens"  # token-limit field QuotaMesh itself sends (Doctor, tests)
    model_list_prefix: str = ""  # prefix stripped from listed model ids for display
    reset_rule: str = ""  # "midnight_pt": daily quota evidence resets at Pacific midnight

    def __post_init__(self) -> None:
        if self.stream_usage not in STREAM_USAGE_MODES:
            raise ValueError(f"Unknown stream_usage mode for {self.id}")
        if self.token_param not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError(f"Unknown token_param for {self.id}")
        if self.reset_rule not in RESET_RULES:
            raise ValueError(f"Unknown reset_rule for {self.id}")


@functools.cache
def registry() -> Mapping[str, Provider]:
    """Return the bundled provider registry (read-only, parsed once per process)."""
    path = Path(__file__).parent / "registry" / "providers.toml"
    rows = tomllib.loads(path.read_text(encoding="utf-8"))["providers"]
    return MappingProxyType({key: Provider(key, **value) for key, value in rows.items()})


def data_directory() -> Path:
    override = os.environ.get("QUOTAMESH_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "QuotaMesh"
    return Path.home() / ".local" / "share" / "quotamesh"


def validate_base_url(value: str) -> str:
    """Disallow credentials in URLs and plaintext non-loopback endpoints."""
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a complete HTTP(S) base URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URLs cannot contain credentials, queries, or fragments.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote API endpoints must use HTTPS.")
    return url


def validate_env_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Environment reference must be a variable name.")
    return value
