"""The .env parser accepts common files safely and never leaks values."""

import pytest

from quotamesh.envfile import (
    MAX_BYTES,
    EnvFileError,
    Secret,
    is_placeholder,
    parse_env,
    provider_for,
    suggested_plan,
)

SAMPLE = (
    "﻿# QuotaMesh keys\r\n"
    "OPENAI_API_KEY=sk-proj-abc123\r\n"
    "export GEMINI_API_KEY = AQ.fake-gemini-key\r\n"
    "\r\n"
    'GROQ_API_KEY="gsk quoted # not a comment"\r\n'
    "NVIDIA_API_KEY='nvapi-$NOT_EXPANDED'  # trailing comment\r\n"
    "MISTRAL_API_KEY=abc#kept # removed comment\r\n"
    "OPENROUTER_API_KEY=${OPENAI_API_KEY}\r\n"
    'ESCAPED="a\\"b\\\\c\\n"\r\n'
    "EMPTY=\r\n"
)


def values(parsed):
    return {name: entry.value.reveal() for name, entry in parsed.entries.items()}


def test_common_syntax_bom_crlf_export_quotes_and_comments():
    parsed = parse_env(SAMPLE)
    assert values(parsed) == {
        "OPENAI_API_KEY": "sk-proj-abc123",
        "GEMINI_API_KEY": "AQ.fake-gemini-key",
        "GROQ_API_KEY": "gsk quoted # not a comment",
        "NVIDIA_API_KEY": "nvapi-$NOT_EXPANDED",
        "MISTRAL_API_KEY": "abc#kept",
        "OPENROUTER_API_KEY": "${OPENAI_API_KEY}",
        "ESCAPED": 'a"b\\c\\n',
        "EMPTY": "",
    }
    assert parsed.entries["GEMINI_API_KEY"].line == 3
    assert parsed.warnings == []


def test_bytes_input_and_bom_are_supported():
    parsed = parse_env(SAMPLE.encode("utf-8"))
    assert values(parsed)["OPENAI_API_KEY"] == "sk-proj-abc123"


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("NO_EQUALS_SIGN", "Expected NAME=value"),
        ("1BAD=secret-value-1", "Invalid variable name"),
        ("BAD-NAME=secret-value-2", "Invalid variable name"),
        ('OPEN="secret-value-3', "Unterminated double-quoted value"),
        ("OPEN='secret-value-4", "Unterminated single-quoted value"),
        ('AFTER="secret-value-5" trailing', "Unexpected text after a quoted value"),
        ("CTRL=secret\x01value-6", "control characters"),
    ],
)
def test_problem_lines_are_skipped_with_content_free_warnings(line, message):
    parsed = parse_env("GOOD=ok\n" + line + "\n")
    assert list(parsed.entries) == ["GOOD"]
    [(number, warning)] = parsed.warnings
    assert number == 2 and message in warning
    assert "secret" not in warning


def test_duplicates_keep_later_value_and_warn():
    parsed = parse_env("A=first-secret\nA=second-secret\n")
    assert values(parsed) == {"A": "second-secret"}
    assert parsed.warnings == [(2, "Duplicate of line 1; the later value is used")]


def test_limits_reject_oversized_or_binary_files_without_content():
    with pytest.raises(EnvFileError) as too_big:
        parse_env("A=" + "x" * MAX_BYTES)
    assert "x" * 10 not in str(too_big.value)
    with pytest.raises(EnvFileError):
        parse_env("A=1\n" * 1001)
    with pytest.raises(EnvFileError, match="UTF-8"):
        parse_env(b"A=\xff\xfe\n")
    long_value = parse_env("A=" + "y" * 16385 + "\n")
    assert not long_value.entries and "too long" in long_value.warnings[0][1]


def test_secret_never_prints_its_value():
    secret = Secret("sk-very-private-value")
    for text in (
        repr(secret),
        str(secret),
        f"{secret}",
        repr(parse_env("K=sk-very-private-value")),
    ):
        assert "very-private" not in text
    assert len(secret) == 21 and len(secret.fingerprint) == 8


@pytest.mark.parametrize(
    ("name", "provider"),
    [
        ("OPENAI_API_KEY", "openai"),
        ("GEMINI_API_KEY", "gemini"),
        ("GOOGLE_API_KEY", "gemini"),
        ("GEMINI_API_KEY_2", "gemini"),
        ("GROQ_API_KEY_15", "groq"),
        ("NVIDIA_API_KEY", "nim"),
        ("OPENROUTER_API_KEY", "openrouter"),
        ("GEMINI_API_KEY_0", None),
        ("GEMINI_API_KEY_100", None),
        ("OPENAI_API_KEY_", None),
        ("OPENAI_KEY", None),
        ("QUOTAMESH_KEY", None),
        ("openai_api_key", None),
    ],
)
def test_provider_mapping_supports_numbered_keys(name, provider):
    assert provider_for(name) == provider


def test_plan_suggestions_default_to_blocked_unknown():
    assert suggested_plan("openai") == "PAID"
    assert suggested_plan("gemini") == "FREE"
    assert suggested_plan("mistral") == "UNKNOWN"
    assert suggested_plan(None) == "UNKNOWN"


@pytest.mark.parametrize(
    ("value", "placeholder"),
    [
        ("", True),
        ("   ", True),
        ("your_openai_api_key_here", True),
        ("<paste key>", True),
        ("changeme", True),
        ("xxxxxxxx", True),
        ("...", True),
        ("sk-proj-real-looking", False),
        ("AQ.fake-example", False),
        ("AIzaSyExample0123456789", False),
    ],
)
def test_placeholders_are_detected_but_real_shapes_are_not_validated(value, placeholder):
    assert is_placeholder(value) is placeholder
