"""Measure QuotaMesh gateway overhead against an instant in-process fake upstream.

No network or provider quota is used. The app runs in-process through ASGITransport and
its upstream is an httpx.MockTransport, so the numbers isolate routing, accounting and
SQLite work. Compare runs on the same machine only.

    python scripts/bench_gateway.py --requests 500 --concurrency 20 --json output/bench/before.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from quotamesh.app import create_app

COMPLETION = json.dumps(
    {
        "id": "bench",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }
).encode()


def sse_body(events: int) -> bytes:
    chunks = [b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n']
    for _ in range(events):
        chunks.append(b'data: {"choices":[{"index":0,"delta":{"content":"tok "}}]}\n\n')
    chunks.append(
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":20}}\n\n'
        b"data: [DONE]\n\n"
    )
    return b"".join(chunks)


def upstream(events: int):
    stream_body = sse_body(events)

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=stream_body
            )
        return httpx.Response(200, headers={"content-type": "application/json"}, content=COMPLETION)

    return handler


def prefill(app, rows: int) -> None:
    """Insert synthetic metadata rows so retention pruning cost is visible."""
    if not rows:
        return
    now = datetime.now(UTC)
    values = [
        (
            (now - timedelta(seconds=rows - index)).isoformat(),
            f"{index:032x}",
            1,
            1,
            "custom",
            "bench-model",
            1,
            "OK",
            200,
            "FREE",
            "Bench key",
            "default",
            0,
        )
        for index in range(rows)
    ]
    with app.state.store.connection() as conn:
        conn.executemany(
            """INSERT INTO attempts (ts, request_id, profile_id, attempt_idx, provider_id, model,
            credential_id, outcome, http_status, plan_type, credential_label, profile_slug, streamed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )


async def configure(app) -> dict[str, str]:
    auth = {"Authorization": "Bearer " + app.state.local_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/credentials",
            headers=auth,
            json={
                "provider_id": "custom",
                "label": "Bench key",
                "plan_type": "FREE",
                "secret_value": "fake-bench",
                "base_url": "http://127.0.0.1:8799/v1",
            },
        )
        created.raise_for_status()
        policy = await client.post(
            "/api/policy",
            headers=auth,
            json={"targets": [{"provider_id": "custom", "model": "bench-model"}]},
        )
        policy.raise_for_status()
    return auth


async def run(app, auth, *, stream: bool, requests: int, concurrency: int, warmup: int):
    gate = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    failures = 0
    body = {"model": "qm/default", "messages": [{"role": "user", "content": "hi"}]}
    if stream:
        body["stream"] = True

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60
    ) as client:

        async def one(record: bool) -> None:
            nonlocal failures
            async with gate:
                started = time.perf_counter()
                async with client.stream(
                    "POST", "/v1/chat/completions", headers=auth, json=body
                ) as response:
                    await response.aread()
                elapsed = time.perf_counter() - started
                if response.status_code != 200:
                    failures += 1
                elif record:
                    latencies.append(elapsed * 1000)

        await asyncio.gather(*(one(False) for _ in range(warmup)))
        started = time.perf_counter()
        await asyncio.gather(*(one(True) for _ in range(requests)))
        wall = time.perf_counter() - started
    latencies.sort()

    def pct(p: float) -> float:
        return round(latencies[min(len(latencies) - 1, int(p * len(latencies)))], 2)

    return {
        "mode": "stream" if stream else "nonstream",
        "requests": requests,
        "concurrency": concurrency,
        "failures": failures,
        "throughput_rps": round(requests / wall, 1),
        "mean_ms": round(statistics.fmean(latencies), 2) if latencies else None,
        "p50_ms": pct(0.50) if latencies else None,
        "p95_ms": pct(0.95) if latencies else None,
        "p99_ms": pct(0.99) if latencies else None,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--events", type=int, default=20, help="content events per stream")
    parser.add_argument("--prefill", type=int, default=0, help="synthetic history rows")
    parser.add_argument("--json", type=Path, help="write results to this file")
    args = parser.parse_args()

    results = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data:
        app = create_app(
            Path(data), httpx.MockTransport(upstream(args.events)), allow_test_host=True
        )
        auth = await configure(app)
        prefill(app, args.prefill)
        for stream in (False, True):
            results.append(
                await run(
                    app,
                    auth,
                    stream=stream,
                    requests=args.requests,
                    concurrency=args.concurrency,
                    warmup=args.warmup,
                )
            )
        await app.state.client.aclose()

    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "prefill_rows": args.prefill,
        "results": results,
    }
    for row in results:
        print(
            f"{row['mode']:>9}: {row['throughput_rps']:>7} req/s  p50 {row['p50_ms']} ms  "
            f"p95 {row['p95_ms']} ms  p99 {row['p99_ms']} ms  failures {row['failures']}"
        )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 1 if any(row["failures"] for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
