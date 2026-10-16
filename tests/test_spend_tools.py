"""First-run setup with explicit paid permission, UTC caps and token prices."""

from datetime import UTC, datetime

import httpx
import pytest

from quotamesh.app import create_app


def usage_response(_request):
    return httpx.Response(
        200, json={"choices": [], "usage": {"prompt_tokens": 400_000, "completion_tokens": 100_000}}
    )


@pytest.fixture
async def paid_key(tmp_path):
    app = create_app(tmp_path, httpx.MockTransport(usage_response), allow_test_host=True)
    auth = {"Authorization": "Bearer " + app.state.local_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/credentials",
            headers=auth,
            json={
                "provider_id": "custom",
                "plan_type": "PAID",
                "secret_value": "fake-paid-setup",
                "base_url": "http://127.0.0.1:8799/v1",
            },
        )
        yield app, client, auth, created.json()["credential_id"]


async def chat(client, auth):
    return await client.post(
        "/v1/chat/completions", headers=auth, json={"model": "qm/default", "messages": []}
    )


async def test_setup_saves_paid_permission_caps_and_target_prices(paid_key):
    app, client, auth, identifier = paid_key
    response = await client.post(
        "/api/setup",
        headers=auth,
        json={
            "credential_id": identifier,
            "model": "paid-model",
            "allow_paid": True,
            "paid_daily_cap_usd": 1,
            "paid_monthly_cap_usd": 5,
            "input_price": 2,
            "output_price": 4,
        },
    )
    assert response.status_code == 201
    assert response.json() == {"alias": "qm/default", "allow_paid": True}
    routing = app.state.store.safe_routing(datetime.now(UTC))
    profile, target = routing["profile"], routing["targets"][0]
    assert profile["allow_paid"] == 1 and profile["allow_unknown_price"] == 0
    assert (profile["paid_daily_cap_usd"], profile["paid_monthly_cap_usd"]) == (1, 5)
    assert (target["input_price"], target["output_price"]) == (2, 4)
    # 400k input + 100k output tokens at $2/$4 per million = $1.20: the cap applies next.
    assert (await chat(client, auth)).status_code == 200
    assert app.state.store.safe_routing(datetime.now(UTC))["usage"]["daily"] == pytest.approx(1.2)
    assert (await chat(client, auth)).status_code == 503


async def test_setup_defaults_keep_paid_off_and_prices_unknown(paid_key):
    app, client, auth, identifier = paid_key
    response = await client.post(
        "/api/setup", headers=auth, json={"credential_id": identifier, "model": "m"}
    )
    assert response.json() == {"alias": "qm/default", "allow_paid": False}
    routing = app.state.store.safe_routing(datetime.now(UTC))
    assert (
        routing["profile"]["allow_paid"] == 0 and routing["profile"]["paid_daily_cap_usd"] is None
    )
    assert routing["targets"][0]["input_price"] is None
    assert (await chat(client, auth)).status_code == 403


async def test_setup_unknown_price_override_is_explicit(paid_key):
    app, client, auth, identifier = paid_key
    response = await client.post(
        "/api/setup",
        headers=auth,
        json={
            "credential_id": identifier,
            "model": "m",
            "allow_paid": True,
            "paid_daily_cap_usd": 10,
            "allow_unknown_price": True,
        },
    )
    assert response.status_code == 201
    assert app.state.store.safe_routing(datetime.now(UTC))["profile"]["allow_unknown_price"] == 1
    assert (await chat(client, auth)).status_code == 200


@pytest.mark.parametrize(
    "field,value",
    [
        ("paid_daily_cap_usd", -1),
        ("paid_monthly_cap_usd", 2_000_000),
        ("input_price", "free"),
        ("output_price", -0.5),
        ("allow_paid", "sometimes"),
        ("unexpected", "sensitive-value"),
    ],
)
async def test_setup_validates_spend_fields_like_policy(paid_key, field, value):
    app, client, auth, identifier = paid_key
    response = await client.post(
        "/api/setup", headers=auth, json={"credential_id": identifier, "model": "m", field: value}
    )
    assert response.status_code == 400
    assert response.json()["error"]["fields"][0]["path"] == [field]
    assert "sensitive-value" not in response.text and "sometimes" not in response.text
    assert app.state.store.profiles() == []
