"""Task-page contracts, safe feedback, and isolated demo edge cases."""

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from quotamesh.app import create_app
from quotamesh.demo import GUIDES, SCENARIOS, configure_demo
from quotamesh.routes.dashboard import PAGES
from quotamesh.wallet import snapshot


async def test_task_pages_are_authenticated_local_and_versioned(tmp_path):
    app = create_app(
        tmp_path,
        httpx.MockTransport(lambda r: pytest.fail("Pages must not probe providers")),
        allow_test_host=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for path in PAGES:
            assert (await client.get(path)).status_code == 401
        await client.get("/bootstrap", params={"token": app.state.bootstrap_token})
        for path, (page, _, _) in PAGES.items():
            response = await client.get(path)
            assert response.status_code == 200
            assert f'data-page="{page}"' in response.text
            assert response.text.count('aria-current="page"') == 1
            for asset in re.findall(r'(?:src|href)="(/static/[^\"]+)"', response.text):
                assert re.search(r"\?v=[a-f0-9]{12}$", asset)
                assert (await client.get(asset)).status_code == 200
        assert 'id="credential-form"' not in (await client.get("/connect")).text
        assert 'id="test-form"' not in (await client.get("/access")).text
        assert (await client.get("/profiles?profile=missing")).status_code == 404


async def test_configuration_field_errors_do_not_echo_inputs(tmp_path):
    app = create_app(tmp_path, allow_test_host=True)
    auth = {"Authorization": "Bearer " + app.state.local_key}
    secret = "do-not-echo-this-secret\n"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/credentials",
            headers=auth,
            json={
                "provider_id": "custom",
                "plan_type": "not-a-plan",
                "secret_value": secret,
                "base_url": "http://127.0.0.1:8799/v1",
            },
        )
        assert response.status_code == 400
        assert "do-not-echo" not in response.text
        assert "input" not in response.text and "ctx" not in response.text
        fields = response.json()["error"]["fields"]
        assert any(f["path"] == ["plan_type"] for f in fields)
        response = await client.post(
            "/api/profiles",
            headers=auth,
            json={"slug": "Invalid Slug", "name": "Example", "targets": []},
        )
        assert response.status_code == 400
        assert any(f["path"] == ["slug"] for f in response.json()["error"]["fields"])
        assert "Invalid Slug" not in response.text


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("expired", "expired"),
        ("missing-env", "missing_secret"),
        ("accounting", "accounting_unavailable"),
    ],
)
async def test_demo_explanations_offer_actions_without_upstream_calls(tmp_path, scenario, reason):
    app = create_app(
        tmp_path,
        httpx.MockTransport(lambda r: pytest.fail("Explain must not generate")),
        allow_test_host=True,
    )
    app.state.demo_mode = True
    app.state.demo_base_url = "http://127.0.0.1:8799/v1"
    configure_demo(app, app.state.demo_base_url, scenario)
    auth = {"Authorization": "Bearer " + app.state.local_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        data = (await client.get("/api/explain", headers=auth)).json()
        decision = next(d for d in data["decisions"] if d["skip_reason"] == reason)
        assert decision["recovery_action"]["label"]
        assert decision["recovery_action"]["credential_id"] == decision["credential_id"]
        assert not app.state.store.recent_attempts()
        assert "fake-" not in str(data["profile"])


def test_demo_guides_and_reset_are_complete_and_isolated(tmp_path):
    app = create_app(tmp_path)
    assert set(GUIDES) == set(SCENARIOS)
    for scenario in SCENARIOS:
        configure_demo(app, "http://127.0.0.1:8799/v1", scenario)
        assert app.state.demo_scenario == scenario
        assert GUIDES[scenario]
    configure_demo(app, "http://127.0.0.1:8799/v1", "large")
    assert len(app.state.store.safe_routing(datetime.now(UTC))["credentials"]) == 38
    configure_demo(app, "http://127.0.0.1:8799/v1", "empty")
    assert not app.state.store.safe_routing(datetime.now(UTC))["credentials"]
    assert not app.state.store.profiles()


async def test_expiry_boundary_uses_controlled_time(tmp_path):
    app = create_app(tmp_path, allow_test_host=True)
    configure_demo(app, "http://127.0.0.1:8799/v1", "expired")
    anchor = datetime(2026, 10, 7, 12, tzinfo=UTC)
    app.state.store.edit_credential(1, {"trial_expires_at": anchor.isoformat()})
    assert not snapshot(app.state.store, anchor - timedelta(seconds=1))["buckets"]["TRIAL_CREDIT"][
        0
    ]["keys"][0]["expired"]
    assert snapshot(app.state.store, anchor)["buckets"]["TRIAL_CREDIT"][0]["keys"][0]["expired"]
