"""Validated local configuration; upstream requests remain raw JSON."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from quotamesh.config import registry

Money = Annotated[float, Field(ge=0, le=1_000_000, allow_inf_nan=False)]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str
    model: Annotated[str, Field(min_length=1, max_length=200)]
    credential_id: Annotated[int, Field(ge=1)] | None = None
    input_price: Money | None = None
    output_price: Money | None = None


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_paid: bool = False
    allow_trial: bool = True
    allow_unknown_price: bool = False
    enabled: bool = True
    max_attempts: Annotated[int, Field(ge=1, le=20)] = 5
    nonstream_deadline_s: Annotated[float, Field(ge=0.05, le=300, allow_inf_nan=False)] = 45
    first_event_timeout_s: Annotated[float, Field(ge=0.05, le=300, allow_inf_nan=False)] = 30
    paid_daily_cap_usd: Money | None = None
    paid_monthly_cap_usd: Money | None = None
    targets: Annotated[list[Target], Field(min_length=1, max_length=30)]

    def validate_targets(self, credentials):
        for target in self.targets:
            if target.provider_id not in registry() or any(
                ord(c) < 32 or ord(c) == 127 for c in target.model
            ):
                raise ValueError("Invalid target provider or model")
            if target.credential_id is not None and not any(
                c["id"] == target.credential_id and c["provider_id"] == target.provider_id
                for c in credentials
            ):
                raise ValueError("Pinned credential must belong to the target provider")


class CredentialAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["reset", "enable", "disable"]


def expiry(value):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value))
    except ValueError:
        raise ValueError("Expiry must be an ISO date or timestamp") from None
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return date.astimezone(UTC).isoformat()


class ProjectProfile(Policy):
    slug: Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]

    def validate_targets(self, credentials):
        super().validate_targets(credentials)
        if any(ord(c) < 32 or ord(c) == 127 for c in self.name) or not self.name.strip():
            raise ValueError("Profile name must be printable")
