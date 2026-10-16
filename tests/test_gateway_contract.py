"""Blocked responses explain themselves and /v1/models matches the OpenAI list shape."""

import httpx
from test_phase_two import call, setup


async def test_blocked_response_names_profile_and_explains_candidates(tmp_path):
    def handler(request):
        return httpx.Response(
            429, headers={"retry-after": "30"}, json={"error": {"code": "rate_limit_exceeded"}}
        )

    _app, client, auth = await setup(tmp_path, handler, keys=[{}])
    async with client:
        first = await call(client, auth)
        assert first.status_code == 429
        blocked = await call(client, auth)
    assert blocked.status_code == 429
    assert blocked.headers["X-QuotaMesh-Profile"] == "default"
    assert len(blocked.headers["X-QuotaMesh-Request-Id"]) == 32
    assert 0 < int(blocked.headers["Retry-After"]) <= 30
    [candidate] = blocked.json()["quotamesh"]["candidates"]
    assert candidate["skip_reason"] == "cooldown"
    assert candidate["explanation"]
    assert candidate["recovery_action"]["code"]


async def test_models_list_and_single_alias_lookup(tmp_path):
    _app, client, auth = await setup(tmp_path, lambda request: httpx.Response(200, json={}))
    async with client:
        listing = (await client.get("/v1/models", headers=auth)).json()
        [item] = listing["data"]
        assert item["id"] == "qm/default" and item["object"] == "model"
        assert isinstance(item["created"], int) and item["owned_by"] == "quotamesh"
        one = await client.get("/v1/models/qm/default", headers=auth)
        assert one.status_code == 200 and one.json() == item
        missing = await client.get("/v1/models/qm/nope", headers=auth)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "model_not_found"
        unauthorized = await client.get("/v1/models/qm/default")
        assert unauthorized.status_code == 401
