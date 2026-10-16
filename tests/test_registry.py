"""The bundled provider registry loads, is read-only and only carries verified quirks."""

import pytest

from quotamesh.config import Provider, registry


def test_every_registry_row_loads_with_safe_defaults():
    providers = registry()
    assert "qm" not in providers, "qm/ is reserved for profile aliases"
    for provider_id, provider in providers.items():
        assert provider.id == provider_id
        assert provider.base_url.startswith("https://") or provider_id == "custom"
    assert registry() is providers, "registry is parsed once per process"
    with pytest.raises(TypeError):
        providers["openai"] = providers["custom"]


def test_quirks_are_explicit_and_evidence_backed():
    providers = registry()
    assert providers["openai"].stream_usage == "inject"
    assert providers["openai"].token_param == "max_completion_tokens"
    assert providers["gemini"].model_list_prefix == "models/"
    assert providers["gemini"].reset_rule == "midnight_pt"
    for unverified in ("groq", "nim", "anthropic", "xai", "openrouter", "cerebras", "mistral"):
        provider = providers[unverified]
        assert (provider.stream_usage, provider.token_param, provider.reset_rule) == (
            "passthrough",
            "max_tokens",
            "",
        )


@pytest.mark.parametrize(
    "field", [{"stream_usage": "always"}, {"token_param": "tokens"}, {"reset_rule": "utc"}]
)
def test_unknown_quirk_values_are_rejected(field):
    with pytest.raises(ValueError):
        Provider("x", "X", "https://example.test/v1", "2026-10-16", **field)
