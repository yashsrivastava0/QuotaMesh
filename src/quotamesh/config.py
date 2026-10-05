"""Local configuration and dated provider registry."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    last_verified: str


def registry() -> dict[str, Provider]:
    path = Path(__file__).parent / "registry" / "providers.toml"
    rows = tomllib.loads(path.read_text(encoding="utf-8"))["providers"]
    return {key: Provider(key, **value) for key, value in rows.items()}


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
