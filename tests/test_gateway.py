import json
import sqlite3

import httpx
import pytest

from quotamesh.app import create_app


def configured_app(tmp_path, handler, *, plan="FREE", allow_paid=False):
    upstream = httpx.MockTransport(handler)
    app = create_app(tmp_path, upstream, allow_test_host=True)
    local_key = app.state.store.gateway_key()
    configuration = {
        "provider_id": "custom",
        "label": "Local fake",
        "plan_type": plan,
        "model": "model-from-upstream",
        "base_url": "http://127.0.0.1:8799/v1",
        "secret_value": "provider-secret-123",
        "allow_paid": allow_paid,
    }
    return app, local_key, configuration


@pytest.mark.asyncio
async def test_bootstrap_setup_and_nonstream_passthrough(tmp_path):
    sent = []

    def upstream(request):
        sent.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "custom_extension": body["custom_extension"],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    app, key, configuration = configured_app(tmp_path, upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/")).status_code == 401
        bootstrap = await client.get(f"/bootstrap?token={app.state.bootstrap_token}")
        assert bootstrap.status_code == 303 and bootstrap.headers["location"] == "/"
        assert "httponly" in bootstrap.headers["set-cookie"].lower()
        assert "samesite=strict" in bootstrap.headers["set-cookie"].lower()
        assert (await client.get("/bootstrap?token=used")).status_code == 403
        saved = await client.post(
            "/api/connection", json=configuration, headers={"Authorization": f"Bearer {key}"}
        )
        assert saved.status_code == 201
        assert "provider-secret-123" not in saved.text
        assert (await client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})).json()[
            "data"
        ][0]["id"] == "qm/default"
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "qm/default",
                "messages": [{"role": "user", "content": "sensitive prompt"}],
                "custom_extension": {"future_field": True},
            },
        )
        assert response.status_code == 200
        assert response.json()["model"] == "model-from-upstream"
        assert response.json()["custom_extension"] == {"future_field": True}
        assert response.headers["x-quotamesh-attempts"] == "1"
        assert sent[0].headers["authorization"] == "Bearer provider-secret-123"
        assert json.loads(sent[0].content)["messages"][0]["content"] == "sensitive prompt"
        status = (await client.get("/api/status", headers={"Authorization": f"Bearer {key}"})).text
        page = (await client.get("/")).text
        assert "provider-secret-123" not in status + page
        assert "sensitive prompt" not in status + page
        assert "http://testserver/v1" in page
        assert "model-from-upstream" in status
    with sqlite3.connect(tmp_path / "quotamesh.db") as conn:
        attempts = conn.execute(
            "SELECT outcome,input_tokens,output_tokens FROM attempts"
        ).fetchall()
        assert attempts == [("OK", 2, 3)]
        assert "sensitive prompt" not in " ".join(
            row[0] or "" for row in conn.execute("SELECT sql FROM sqlite_master")
        )


@pytest.mark.asyncio
async def test_paid_blocked_until_explicitly_enabled(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": []})

    app, key, configuration = configured_app(tmp_path, upstream, plan="PAID")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        auth = {"Authorization": f"Bearer {key}"}
        assert (
            await client.post("/api/connection", json=configuration, headers=auth)
        ).status_code == 201
        payload = {"model": "qm/default", "messages": []}
        assert (
            await client.post("/v1/chat/completions", json=payload, headers=auth)
        ).status_code == 403
        assert calls == []
        configuration["allow_paid"] = True
        assert (
            await client.post("/api/connection", json=configuration, headers=auth)
        ).status_code == 201
        assert (
            await client.post("/v1/chat/completions", json=payload, headers=auth)
        ).status_code == 200
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_stream_commit_and_first_event_error(tmp_path):
    class EventStream(httpx.AsyncByteStream):
        def __init__(self, body):
            self.body = body

        async def __aiter__(self):
            yield self.body

    response_event = b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
    done_event = b"data: [DONE]\n\n"
    app, key, configuration = configured_app(
        tmp_path,
        lambda request: httpx.Response(
            200,
            stream=EventStream(response_event + done_event),
            headers={"content-type": "text/event-stream"},
        ),
    )
    auth = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.post("/api/connection", json=configuration, headers=auth)
        response = await client.post(
            "/v1/chat/completions", json={"model": "qm/default", "stream": True}, headers=auth
        )
        assert response.status_code == 200
        assert response.content == response_event + done_event
        assert response.headers["x-quotamesh-provider"] == "custom"
    assert app.state.store.recent_attempts()[0]["outcome"] == "OK"

    error_app, error_key, error_config = configured_app(
        tmp_path / "error",
        lambda request: httpx.Response(
            200,
            stream=EventStream(b'data: {"error":{"message":"bad"}}\n\n'),
            headers={"content-type": "text/event-stream"},
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=error_app), base_url="http://testserver"
    ) as client:
        auth = {"Authorization": f"Bearer {error_key}"}
        await client.post("/api/connection", json=error_config, headers=auth)
        response = await client.post(
            "/v1/chat/completions", json={"model": "qm/default", "stream": True}, headers=auth
        )
        assert response.status_code == 502
        assert error_app.state.store.recent_attempts()[0]["outcome"] == "PRECOMMIT_ERROR"


@pytest.mark.asyncio
async def test_host_origin_auth_and_redirect_defenses(tmp_path):
    sent = []

    def redirecting_upstream(request):
        sent.append(request)
        return httpx.Response(
            307, headers={"location": "https://other.example/v1/chat/completions"}
        )

    app, key, configuration = configured_app(tmp_path, redirecting_upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        auth = {"Authorization": f"Bearer {key}"}
        assert (
            await client.post(
                "/api/connection", json=configuration, headers=auth, follow_redirects=False
            )
        ).status_code == 201
        assert (
            await client.post(
                "/api/connection",
                json=configuration,
                headers={**auth, "Origin": "https://evil.example"},
            )
        ).status_code == 403
        assert (
            await client.post("/v1/chat/completions", json={"model": "qm/default"})
        ).status_code == 401
        assert (
            await client.post(
                "/v1/chat/completions",
                json={"model": "qm/default"},
                headers={**auth, "Host": "evil.example"},
            )
        ).status_code == 403
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qm/default"},
            headers=auth,
            follow_redirects=False,
        )
        assert response.status_code == 502
        assert len(sent) == 1
        assert sent[0].url.host == "127.0.0.1"


def test_custom_url_requires_https_for_remote(tmp_path):
    app, _, _ = configured_app(tmp_path, lambda request: httpx.Response(200))
    assert app.state.store.gateway_key()
    from quotamesh.config import validate_base_url

    with pytest.raises(ValueError):
        validate_base_url("http://remote.example/v1")
    with pytest.raises(ValueError):
        validate_base_url("https://user:pass@remote.example/v1")


@pytest.mark.asyncio
async def test_large_body_stops_before_upstream(tmp_path, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200)

    app, key, configuration = configured_app(tmp_path, upstream)
    monkeypatch.setattr("quotamesh.app.MAX_BODY_BYTES", 32)
    auth = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.post("/api/connection", json=configuration, headers=auth)
        response = await client.post("/v1/chat/completions", content=b"{" + b"a" * 33, headers=auth)
        assert response.status_code == 413
    assert calls == []


@pytest.mark.asyncio
async def test_midstream_failure_keeps_committed_target(tmp_path):
    calls = []

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            raise httpx.ReadError("fake stream cut")

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            200, stream=InterruptedStream(), headers={"content-type": "text/event-stream"}
        )

    app, key, configuration = configured_app(tmp_path, upstream)
    auth = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.post("/api/connection", json=configuration, headers=auth)
        response = await client.post(
            "/v1/chat/completions", json={"model": "qm/default", "stream": True}, headers=auth
        )
        assert response.status_code == 200
        assert b"event: error" in response.content
        assert len(calls) == 1
    assert app.state.store.recent_attempts()[0]["outcome"] == "MID_STREAM_FAILURE"
