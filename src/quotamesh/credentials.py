"""Strict wallet metadata validation; secret values never appear in responses."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quotamesh.config import registry, validate_base_url, validate_env_name
from quotamesh.policy import Money, expiry

Text = Annotated[str, Field(min_length=1, max_length=100)]


class CredentialPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str | None = None
    label: Text | None = None
    plan_type: Literal["FREE", "TRIAL_CREDIT", "PAID", "UNKNOWN"] | None = None
    account_label: Text | None = None
    quota_group: Text | None = None
    priority: Annotated[int, Field(ge=-10000, le=10000)] | None = None
    starting_credit_usd: Money | None = None
    trial_expires_at: str | None = None
    base_url: str | None = None
    secret_value: Annotated[str, Field(min_length=1, max_length=16384)] | None = None
    env_name: str | None = None
    enabled: bool | None = None

    @field_validator(
        "label", "account_label", "quota_group", "secret_value", "env_name", mode="before"
    )
    @classmethod
    def printable(cls, value):
        if value is not None:
            value = str(value).strip()
            if not value and cls.__name__ == "CredentialCreate":
                return None
            if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Use printable text")
        return value

    @field_validator("trial_expires_at")
    @classmethod
    def timestamp(cls, value):
        return expiry(value)

    def validated(self, existing=None):
        values = self.model_dump(exclude_unset=True)
        for name in ("provider_id", "label", "plan_type", "priority", "base_url", "enabled"):
            if name in values and values[name] is None:
                raise ValueError("Required fields cannot be cleared")
        secret, env = values.get("secret_value"), values.get("env_name")
        if ("secret_value" in values or "env_name" in values) and bool(secret) == bool(env):
            raise ValueError("Supply exactly one secret or environment reference when rotating")
        if env:
            validate_env_name(env)
        provider = values.get("provider_id", (existing or {}).get("provider_id"))
        if provider not in registry():
            raise ValueError("Unsupported provider")
        if provider != "custom":
            values["base_url"] = registry()[provider].base_url
        elif "base_url" in values or not existing or provider != existing["provider_id"]:
            values["base_url"] = validate_base_url(values.get("base_url") or "")
        return values


class CredentialCreate(CredentialPatch):
    provider_id: str
    label: Text
    plan_type: Literal["FREE", "TRIAL_CREDIT", "PAID", "UNKNOWN"]
    # Legacy Phase 1/2 clients supplied target fields when creating a key. They are
    # accepted for compatibility, but profile targets own model and price policy.
    model: str | None = None
    allow_paid: bool = False
    input_price: Money | None = None
    output_price: Money | None = None

    def validated(self, existing=None):
        values = super().validated(existing)
        if bool(values.get("secret_value")) == bool(values.get("env_name")):
            raise ValueError("Supply exactly one secret or environment reference")
        values.setdefault("secret_value", None)
        values.setdefault("env_name", None)
        return values
