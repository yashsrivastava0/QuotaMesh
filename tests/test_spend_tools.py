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


async def test_reconcile_clears_unknown_spend_and_records_the_manual_amount(paid_key):
    app, client, auth, identifier = paid_key
    await client.post(
        "/api/setup",
        headers=auth,
        json={
            "credential_id": identifier,
            "model": "paid-model",
            "allow_paid": True,
            "paid_daily_cap_usd": 1,
            "input_price": 2,
            "output_price": 4,
        },
    )
    today = datetime.now(UTC).date().isoformat()
    with app.state.store.connection() as conn:
        conn.execute(
            "INSERT INTO daily_usage(day,profile_id,cost_usd,unknown_cost) VALUES (?,1,0,1)",
            (today,),
        )
    [blocked] = (await client.get("/api/explain", headers=auth)).json()["decisions"]
    assert blocked["skip_reason"] == "unknown_spend"
    for body, status in (
        ({"amount_usd": -1, "scope": "today"}, 400),
        ({"amount_usd": 0.25, "scope": "week"}, 400),
        ({"amount_usd": True}, 400),
    ):
        response = await client.post("/api/profiles/default/reconcile", headers=auth, json=body)
        assert response.status_code == status
    missing = await client.post(
        "/api/profiles/nope/reconcile", headers=auth, json={"amount_usd": 0, "scope": "today"}
    )
    assert missing.status_code == 404
    assert (
        await client.post("/api/profiles/default/reconcile", json={"amount_usd": 0})
    ).status_code == 401
    done = await client.post(
        "/api/profiles/default/reconcile", headers=auth, json={"amount_usd": 0.25, "scope": "today"}
    )
    assert done.status_code == 200 and done.json() == {"cleared_days": 1, "amount_usd": 0.25}
    [decision] = (await client.get("/api/explain", headers=auth)).json()["decisions"]
    assert decision["eligible"]
    with app.state.store.connection() as conn:
        row = conn.execute(
            "SELECT cost_usd, unknown_cost FROM daily_usage WHERE day=? AND profile_id=1", (today,)
        ).fetchone()
    assert (row["cost_usd"], row["unknown_cost"]) == (0.25, 0)


async def test_accounting_acknowledge_requires_confirmation_and_reopens_paid(paid_key):
    app, client, auth, _ = paid_key
    app.state.accounting_failed = True
    app.state.store.accounting_marker.touch()
    unconfirmed = await client.post("/api/accounting/acknowledge", headers=auth, json={})
    assert unconfirmed.status_code == 400 and app.state.accounting_failed
    confirmed = await client.post(
        "/api/accounting/acknowledge", headers=auth, json={"confirm": True}
    )
    assert confirmed.status_code == 200 and confirmed.json() == {"accounting_failed": False}
    assert not app.state.accounting_failed
    assert not app.state.store.accounting_marker.exists()


def test_start_env_file_loads_only_provider_keys_without_printing_them(
    tmp_path, monkeypatch, capsys
):
    from quotamesh.cli import load_env_file

    env = tmp_path / ".env"
    env.write_text(
        "GEMINI_API_KEY=gemini-private-value\nOTHER_SECRET=not-a-provider\n"
        "OPENAI_API_KEY=your_openai_api_key_here\nGROQ_API_KEY=groq-new-value\n",
        encoding="utf-8",
    )
    for name in ("GEMINI_API_KEY", "OTHER_SECRET", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "already-set")
    load_env_file(env)
    import os

    assert os.environ["GEMINI_API_KEY"] == "gemini-private-value"
    assert "OTHER_SECRET" not in os.environ and "OPENAI_API_KEY" not in os.environ
    assert os.environ["GROQ_API_KEY"] == "already-set"
    printed = capsys.readouterr().out
    assert "GEMINI_API_KEY" in printed and "private-value" not in printed


def test_start_refuses_a_busy_port_before_printing_a_link():
    import socket

    from typer.testing import CliRunner

    from quotamesh.cli import app as cli
    from quotamesh.cli import port_available

    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        assert not port_available(port)
        result = CliRunner().invoke(cli, ["start", "--port", str(port), "--no-browser"])
    assert result.exit_code == 1
    assert "busy" in result.stdout and "bootstrap" not in result.stdout
