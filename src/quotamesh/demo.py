"""Small scripted OpenAI-shaped upstream for Phase 1 smoke tests."""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="QuotaMesh fake upstream")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    key = request.headers.get("authorization", "").removeprefix("Bearer ")
    payload = await request.json()
    if key == "fake-401":
        return JSONResponse({"error": {"message": "fake invalid key"}}, status_code=401)
    if key == "fake-429":
        return JSONResponse(
            {"error": {"message": "fake rate limit"}}, status_code=429, headers={"Retry-After": "5"}
        )
    if key not in {"fake-200", "fake-sse"}:
        return JSONResponse({"error": {"message": "use fake-200 or fake-sse"}}, status_code=401)
    if payload.get("stream"):

        async def events():
            chunk = {
                "id": "fake-1",
                "object": "chat.completion.chunk",
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hello from fake upstream"},
                        "finish_reason": None,
                    }
                ],
            }
            yield "data: " + json.dumps(chunk) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")
    return {
        "id": "fake-1",
        "object": "chat.completion",
        "model": payload["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from fake upstream"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 4, "total_tokens": 5},
    }
