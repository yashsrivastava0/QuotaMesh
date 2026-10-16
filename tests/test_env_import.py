""".env import: parse previews and file imports never echo or log key values."""

from datetime import UTC, datetime

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from quotamesh.app import create_app
from quotamesh.cli import app as cli

VALUES = {
    "GEMINI_API_KEY": "fake-gemini-value-0001",
    "GEMINI_API_KEY_2": "fake-gemini-value-0002",
    "NVIDIA_API_KEY": "fake-nim-configured-0003",
}
ENV_TEXT = """# Local provider keys
GEMINI_API_KEY=fake-gemini-value-0001
GEMINI_API_KEY_2="fake-gemini-value-0002"
GOOGLE_API_KEY=fake-gemini-value-0001
OPENAI_API_KEY=your-openai-key-here
GROQ_API_KEY=fake groq with spaces
DATABASE_URL=postgres://user:fake-db-pass@localhost/db
NVIDIA_API_KEY=fake-nim-configured-0003
not a valid line
"""
FORBIDDEN = (
    *VALUES.values(),
    "your-openai-key-here",
    "fake groq with spaces",
    "fake-db-pass",
    "postgres://",
)


def assert_private(text):
    for value in FORBIDDEN:
        assert value not in text


@pytest.fixture
async def gateway(tmp_path):
    app = create_app(
        tmp_path,
        httpx.MockTransport(lambda r: pytest.fail("Import must not call a provider")),
        allow_test_host=True,
    )
    auth = {"Authorization": "Bearer " + app.state.local_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        saved = await client.post(
            "/api/credentials",
            headers=auth,
            json={
                "provider_id": "nim",
                "plan_type": "TRIAL_CREDIT",
                "secret_value": VALUES["NVIDIA_API_KEY"],
            },
        )
        assert saved.status_code == 201
        yield app, client, auth


async def test_parse_reports_statuses_without_values(gateway):
    app, client, auth = gateway
    response = await client.post("/api/environment/parse", headers=auth, json={"content": ENV_TEXT})
    assert response.status_code == 200
    assert_private(response.text)
    data = response.json()
    statuses = {entry["name"]: entry["status"] for entry in data["entries"]}
    assert statuses == {
        "GEMINI_API_KEY": "new",
        "GEMINI_API_KEY_2": "new",
        "GOOGLE_API_KEY": "duplicate",
        "OPENAI_API_KEY": "placeholder",
        "GROQ_API_KEY": "invalid",
        "DATABASE_URL": "unsupported",
        "NVIDIA_API_KEY": "configured",
    }
    first = data["entries"][0]
    assert first["line"] == 2 and first["provider_id"] == "gemini"
    assert first["suggested_plan"] == "FREE" and first["length"] == len(VALUES["GEMINI_API_KEY"])
    assert len(first["fingerprint"]) == 8 and int(first["fingerprint"], 16) >= 0
    unsupported = next(e for e in data["entries"] if e["name"] == "DATABASE_URL")
    assert unsupported["fingerprint"] is None and unsupported["provider_id"] is None
    assert data["warnings"] == [{"line": 9, "message": "Expected NAME=value; line ignored"}]
    assert app.state.store.recent_attempts() == []


async def test_import_file_saves_values_with_plans_and_skips_with_reasons(gateway):
    app, client, auth = gateway
    items = [
        {"name": "GEMINI_API_KEY", "plan_type": "FREE"},
        {"name": "GEMINI_API_KEY_2", "plan_type": "PAID", "label": "Gemini second"},
        {"name": "GOOGLE_API_KEY", "plan_type": "FREE"},
        {"name": "OPENAI_API_KEY", "plan_type": "PAID"},
        {"name": "NVIDIA_API_KEY", "plan_type": "TRIAL_CREDIT"},
        {"name": "MISSING_API_KEY"},
        {"name": "GEMINI_API_KEY", "plan_type": "FREE"},
    ]
    response = await client.post(
        "/api/environment/import-file",
        headers=auth,
        json={"content": ENV_TEXT, "store": "value", "items": items},
    )
    assert response.status_code == 201
    assert_private(response.text)
    data = response.json()
    assert [(row["name"], row["plan_type"]) for row in data["imported"]] == [
        ("GEMINI_API_KEY", "FREE"),
        ("GEMINI_API_KEY_2", "PAID"),
    ]
    assert {row["name"]: row["reason"] for row in data["skipped"][:4]} == {
        "GOOGLE_API_KEY": "duplicate",
        "OPENAI_API_KEY": "placeholder",
        "NVIDIA_API_KEY": "configured",
        "MISSING_API_KEY": "not_found",
    }
    assert data["skipped"][-1] == {"name": "GEMINI_API_KEY", "reason": "duplicate"}
    credentials = {c["id"]: c for c in app.state.store.routing_snapshot(datetime.now(UTC))[2]}
    first, second = (credentials[row["credential_id"]] for row in data["imported"])
    assert first["secret_value"] == VALUES["GEMINI_API_KEY"] and first["env_name"] is None
    assert first["label"] == "Google Gemini (OpenAI compatibility) / GEMINI_API_KEY"
    assert second["label"] == "Gemini second" and second["plan_type"] == "PAID"
    assert app.state.store.profiles() == []  # No targets or paid permission are created.
    again = await client.post("/api/environment/parse", headers=auth, json={"content": ENV_TEXT})
    statuses = {entry["name"]: entry["status"] for entry in again.json()["entries"]}
    assert statuses["GEMINI_API_KEY"] == statuses["GEMINI_API_KEY_2"] == "configured"


async def test_reference_import_saves_names_and_warns_about_the_server_environment(
    gateway, monkeypatch
):
    app, client, auth = gateway
    monkeypatch.setenv("GEMINI_API_KEY", "fake-other-server-value")
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    response = await client.post(
        "/api/environment/import-file",
        headers=auth,
        json={
            "content": ENV_TEXT,
            "store": "reference",
            "items": [{"name": "GEMINI_API_KEY"}, {"name": "GEMINI_API_KEY_2"}],
        },
    )
    assert response.status_code == 201
    assert_private(response.text)
    assert "fake-other-server-value" not in response.text
    data = response.json()
    assert [row["plan_type"] for row in data["imported"]] == ["UNKNOWN", "UNKNOWN"]
    warnings = {row["name"]: row["message"] for row in data["warnings"]}
    assert "different value" in warnings["GEMINI_API_KEY"]
    assert "Not set" in warnings["GEMINI_API_KEY_2"]
    saved = app.state.store.routing_snapshot(datetime.now(UTC))[2][-1]
    assert saved["secret_value"] is None and saved["env_name"] == "GEMINI_API_KEY_2"


@pytest.mark.parametrize(
    "body,status",
    [
        ({"content": "A=" + "x" * (64 * 1024)}, 413),
        ({"content": "A=1\n" * 1001}, 400),
        ({"content": 5}, 400),
        ({"content": "A=1", "path": "/etc/passwd"}, 400),
    ],
)
async def test_parse_limits_and_errors_are_content_free(gateway, body, status):
    _, client, auth = gateway
    response = await client.post("/api/environment/parse", headers=auth, json=body)
    assert response.status_code == status
    assert "x" * 50 not in response.text and "/etc/passwd" not in response.text


async def test_oversized_request_body_is_rejected_before_parsing(gateway):
    _, client, auth = gateway
    response = await client.post(
        "/api/environment/parse",
        headers=auth | {"Content-Type": "application/json"},
        content=b'{"content": "' + b"\\u0041" * 200_000 + b'"}',
    )
    assert response.status_code == 413


async def test_import_validates_every_item_before_saving_any(gateway):
    app, client, auth = gateway
    response = await client.post(
        "/api/environment/import-file",
        headers=auth,
        json={
            "content": ENV_TEXT,
            "items": [
                {"name": "GEMINI_API_KEY"},
                {"name": "GEMINI_API_KEY_2", "label": "bad\nlabel"},
            ],
        },
    )
    assert response.status_code == 400 and "bad" not in response.text
    assert len(app.state.store.routing_snapshot(datetime.now(UTC))[2]) == 1


@pytest.mark.parametrize("path", ["/api/environment/parse", "/api/environment/import-file"])
async def test_environment_file_endpoints_require_authorization(gateway, path):
    _, client, auth = gateway
    body = {"content": ENV_TEXT, "items": [{"name": "GEMINI_API_KEY"}]}
    assert (await client.post(path, json=body)).status_code == 401
    foreign = await client.post(path, json=body, headers=auth | {"Origin": "https://evil.invalid"})
    assert foreign.status_code == 403


def local_server(app, monkeypatch):
    """Route the CLI's local API calls to the in-process app with the bearer key."""
    client = TestClient(app, base_url="http://testserver")
    auth = {"Authorization": "Bearer " + app.state.local_key}
    calls = []

    def local_api(path, *, port=8787, body=None, method="GET"):
        calls.append((method, path, body))
        response = client.request(method, path, json=body, headers=auth)
        assert response.is_success, response.text
        return response.json()

    monkeypatch.setattr("quotamesh.cli.local_api", local_api)
    return calls


def test_cli_import_env_file_uses_suggestions_and_overrides(gateway, tmp_path, monkeypatch):
    app, _, _ = gateway
    calls = local_server(app, monkeypatch)
    env_file = tmp_path / "keys.env"
    env_file.write_text(ENV_TEXT, encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        ["import-env", "--file", str(env_file), "--yes", "--plan", "GEMINI_API_KEY_2=PAID"],
    )
    assert result.exit_code == 0, result.output
    assert_private(result.output)
    assert "GEMINI_API_KEY" in result.output and "placeholder" in result.output
    assert "Imported GEMINI_API_KEY as credential" in result.output
    assert "(gemini, FREE)" in result.output and "(gemini, PAID)" in result.output
    assert "Line 9: Expected NAME=value" in result.output
    assert "quotamesh doctor" in result.output
    assert [path for _, path, _ in calls] == [
        "/api/environment/parse",
        "/api/environment/import-file",
    ]
    assert str(env_file) not in str(calls)  # The server receives content, never a path.


def test_cli_import_env_file_prompts_per_key(gateway, tmp_path, monkeypatch):
    app, _, _ = gateway
    local_server(app, monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_TEXT, encoding="utf-8")
    result = CliRunner().invoke(
        cli, ["import-env", "--file", str(env_file)], input="nonsense\nTRIAL_CREDIT\nskip\n"
    )
    assert result.exit_code == 0, result.output
    assert_private(result.output)
    assert "(gemini, TRIAL_CREDIT)" in result.output
    assert "GEMINI_API_KEY_2 as credential" not in result.output
    assert len(app.state.store.routing_snapshot(datetime.now(UTC))[2]) == 2


def test_cli_import_env_rejects_bad_options(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_TEXT, encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(cli, ["import-env", "--file", str(env_file), "--plan", "X"]).exit_code == 2
    assert (
        runner.invoke(cli, ["import-env", "--file", str(env_file), "--store", "disk"]).exit_code
        == 2
    )
    assert runner.invoke(cli, ["import-env", "--file", str(tmp_path / "absent")]).exit_code == 1


def test_cli_legacy_name_form_still_imports_references(monkeypatch):
    calls = []

    def local_api(path, *, port=8787, body=None, method="GET"):
        calls.append(path)
        if path == "/api/environment":
            return {
                "variables": [
                    {
                        "env_name": "OPENAI_API_KEY",
                        "provider_id": "openai",
                        "present": True,
                        "configured": False,
                    }
                ]
            }
        return {"imported": [{"env_name": "OPENAI_API_KEY", "credential_id": 1}], "skipped": []}

    monkeypatch.setattr("quotamesh.cli.local_api", local_api)
    result = CliRunner().invoke(cli, ["import-env", "OPENAI_API_KEY"])
    assert result.exit_code == 0 and calls == ["/api/environment", "/api/environment/import"]
