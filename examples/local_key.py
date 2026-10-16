"""Find the local QuotaMesh gateway key for the examples.

Order: QUOTAMESH_API_KEY, then QUOTAMESH_KEY, then the key QuotaMesh saved on this machine
(the same file `quotamesh key` prints), so the examples work without extra setup when they
run on the computer where QuotaMesh runs. This is the local gateway key, not a provider key.
"""

from __future__ import annotations

import os
from pathlib import Path

KEY_HELP = """No local gateway key found (it is not a provider key). Start QuotaMesh once, or set it:
  PowerShell: $env:QUOTAMESH_API_KEY = (.venv\\Scripts\\quotamesh.exe key)
  bash:       export QUOTAMESH_API_KEY="$(.venv/bin/quotamesh key)"
For `quotamesh demo`, use the `quotamesh key --data-dir ...` command it prints."""


def data_directory() -> Path:
    """QuotaMesh's data directory, using the same rules as the server."""
    override = os.environ.get("QUOTAMESH_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "QuotaMesh"
    return Path.home() / ".local" / "share" / "quotamesh"


def local_gateway_key() -> tuple[str | None, str]:
    """Return (key, where it came from); the key is None when nothing was found."""
    for name in ("QUOTAMESH_API_KEY", "QUOTAMESH_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    path = data_directory() / "gateway-key"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, ""
    return (value, str(path)) if value else (None, "")
