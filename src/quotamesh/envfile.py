"""Safe, minimal ``.env`` parsing for importing provider keys.

Values are wrapped in :class:`Secret` and never appear in warnings, errors or reprs.
Only plain ``NAME=value`` lines are supported: no interpolation, command
substitution or multi-line values, so a file can never execute or expand anything.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

MAX_BYTES = 64 * 1024
MAX_LINES = 1000
MAX_VALUE = 16384

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLACEHOLDER = re.compile(
    r"(?i)^(?:your[_\-\s].*|<.*>|\[.*\]|changeme|change[_\-]me|replace[_\-]?me.*|x{3,}"
    r"|\.{3,}|todo|placeholder|none|null|empty|sk-\.\.\.|sk-x+)$"
)

# Provider key names recognised for import. Numbered suffixes (GEMINI_API_KEY_2) let
# one provider contribute several keys to a pool.
PROVIDER_PREFIXES = {
    "OPENAI": "openai",
    "GEMINI": "gemini",
    "GOOGLE": "gemini",
    "GROQ": "groq",
    "NVIDIA": "nim",
    "ANTHROPIC": "anthropic",
    "XAI": "xai",
    "OPENROUTER": "openrouter",
    "CEREBRAS": "cerebras",
    "MISTRAL": "mistral",
}
_PROVIDER_NAME = re.compile(r"^(" + "|".join(PROVIDER_PREFIXES) + r")_API_KEY(?:_([1-9][0-9]?))?$")

# Suggestions only: the user confirms each plan. Paid and unknown plans stay blocked
# until a profile explicitly allows paid use.
SUGGESTED_PLANS = {
    "openai": "PAID",
    "gemini": "FREE",
    "groq": "FREE",
    "nim": "TRIAL_CREDIT",
}


class EnvFileError(ValueError):
    """Unreadable input. Messages never include file content."""


class Secret:
    """A value that does not print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self._value.encode()).hexdigest()[:8]

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return f"Secret(len={len(self._value)}, fp={self.fingerprint})"

    __str__ = __repr__


@dataclass(frozen=True)
class EnvEntry:
    name: str
    value: Secret
    line: int


@dataclass(frozen=True)
class ParsedEnv:
    entries: dict[str, EnvEntry] = field(default_factory=dict)
    warnings: list[tuple[int, str]] = field(default_factory=list)


def provider_for(name: str) -> str | None:
    """Return the provider id for a recognised key variable name."""
    match = _PROVIDER_NAME.fullmatch(name)
    return PROVIDER_PREFIXES[match.group(1)] if match else None


def suggested_plan(provider_id: str | None) -> str:
    return SUGGESTED_PLANS.get(provider_id or "", "UNKNOWN")


def is_placeholder(value: str) -> bool:
    text = value.strip()
    return not text or bool(_PLACEHOLDER.fullmatch(text))


def _value(raw: str) -> tuple[str | None, str | None]:
    """Parse the text after ``=``. Returns (value, problem)."""
    if raw[:1] == '"':
        chars, escaped = [], False
        for index, char in enumerate(raw[1:], start=1):
            if escaped:
                chars.append(char if char in '"\\' else "\\" + char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                rest = raw[index + 1 :]
                break
            else:
                chars.append(char)
        else:
            return None, "Unterminated double-quoted value; multi-line values are not supported"
        value = "".join(chars)
    elif raw[:1] == "'":
        end = raw.find("'", 1)
        if end < 0:
            return None, "Unterminated single-quoted value; multi-line values are not supported"
        value, rest = raw[1:end], raw[end + 1 :]
    else:
        match = re.search(r"\s#", raw)
        return (raw[: match.start()] if match else raw).rstrip(), None
    if rest.strip() and not rest.lstrip().startswith("#"):
        return None, "Unexpected text after a quoted value"
    return value, None


def parse_env(content: str | bytes) -> ParsedEnv:
    """Parse ``.env`` text. Raises :class:`EnvFileError` only for unreadable input."""
    if isinstance(content, bytes):
        if len(content) > MAX_BYTES:
            raise EnvFileError("The file is larger than 64 KiB")
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise EnvFileError("The file is not UTF-8 text") from None
    elif len(content.encode("utf-8", "surrogatepass")) > MAX_BYTES:
        raise EnvFileError("The file is larger than 64 KiB")
    content = content.removeprefix("﻿")
    lines = content.splitlines()
    if len(lines) > MAX_LINES:
        raise EnvFileError("The file has more than 1000 lines")

    parsed = ParsedEnv()
    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith(("export ", "export\t")):
            text = text[7:].lstrip()
        name, separator, raw = text.partition("=")
        name = name.strip()
        if not separator:
            parsed.warnings.append((number, "Expected NAME=value; line ignored"))
            continue
        if not _NAME.fullmatch(name):
            parsed.warnings.append((number, "Invalid variable name; line ignored"))
            continue
        value, problem = _value(raw.strip())
        if problem:
            parsed.warnings.append((number, problem + "; line ignored"))
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            parsed.warnings.append((number, "Value contains control characters; line ignored"))
            continue
        if len(value) > MAX_VALUE:
            parsed.warnings.append((number, "Value is too long; line ignored"))
            continue
        if name in parsed.entries:
            previous = parsed.entries[name].line
            parsed.warnings.append(
                (number, f"Duplicate of line {previous}; the later value is used")
            )
        parsed.entries[name] = EnvEntry(name, Secret(value), number)
    return parsed
