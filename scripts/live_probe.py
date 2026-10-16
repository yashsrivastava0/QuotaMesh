"""Capture real OpenAI/Gemini behaviour as sanitized fixtures (opt-in, uses real keys).

Calls providers directly (QuotaMesh is not involved) with tiny, token-capped requests:
model listings and rejected requests are free; generation calls are budgeted by
--max-calls. Keys are read from a .env file, never printed, and every output file is
checked for key material before it is written. Model output text is redacted.

    python scripts/live_probe.py --env-file .env --out tests/fixtures/providers --max-calls 16
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from quotamesh.envfile import parse_env

OPENAI = "https://api.openai.com/v1"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_NATIVE = "https://generativelanguage.googleapis.com/v1beta"
PROMPT = [{"role": "user", "content": "Reply with the single word OK."}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "Look up an order by id.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    }
]
HEADER_ALLOW = re.compile(r"^(content-type|retry-after|x-ratelimit-.*)$", re.IGNORECASE)
REDACTED_KEYS = {
    "content",
    "arguments",
    "refusal",
    "reasoning_content",
    "thought",
    "thought_signature",
    "obfuscation",
}


class Budget(RuntimeError):
    pass


class Probe:
    def __init__(self, keys: dict[str, str], out: Path, max_calls: int):
        self.keys = keys
        self.out = out
        self.max_calls = max_calls
        self.generation_calls = 0
        self.last_gemini_call = 0.0
        self.summary: dict[str, Any] = {
            "source": f"live probe {datetime.now(UTC).date().isoformat()}",
            "providers": {},
        }
        self.client = httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False)

    # -- safety --------------------------------------------------------------------------
    def assert_clean(self, text: str) -> None:
        for secret in self.keys.values():
            slices = [secret] + [secret[i : i + 16] for i in range(8, max(9, len(secret) - 16), 16)]
            if any(len(part) >= 12 and part in text for part in slices):
                raise SystemExit("Refusing to write output that contains key material")

    def scrub(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: self.scrub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v, key) for v in value]
        if isinstance(value, str):
            if key in REDACTED_KEYS and value:
                return "<redacted>"
            if key == "id":
                return "<id>"
            if key == "system_fingerprint":
                return "<fingerprint>"
            value = re.sub(r"\borg-[A-Za-z0-9]{6,}", "org-<redacted>", value)
            value = re.sub(r"\bproj_[A-Za-z0-9]{6,}", "proj_<redacted>", value)
            value = re.sub(r"projects/[0-9]+", "projects/<redacted>", value)
            value = re.sub(r"\b[0-9]{8,}\b", "<number>", value)
            return value
        if key == "created" and isinstance(value, int):
            return 0
        return value

    def write(self, provider: str, case: str, data: dict[str, Any]) -> None:
        data = {"source": self.summary["source"], "provider": provider, "case": case, **data}
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self.assert_clean(text)
        path = self.out / provider / f"{case}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    # -- transport -----------------------------------------------------------------------
    def headers_for(self, style: str, secret: str) -> dict[str, str]:
        if style == "x-goog-api-key":
            return {"x-goog-api-key": secret}
        return {"Authorization": "Bearer " + secret}

    def pace(self, provider: str, generation: bool) -> None:
        if not generation:
            return
        if self.generation_calls >= self.max_calls:
            raise Budget(f"generation budget of {self.max_calls} calls reached")
        self.generation_calls += 1
        if provider == "gemini":
            wait = 5 - (time.monotonic() - self.last_gemini_call)
            if wait > 0:
                time.sleep(wait)
            self.last_gemini_call = time.monotonic()

    def body_of(self, response: httpx.Response) -> Any:
        try:
            return self.scrub(response.json())
        except ValueError:
            return self.scrub(response.text[:4000])

    def kept_headers(self, response: httpx.Response) -> dict[str, str]:
        return {k.lower(): v for k, v in response.headers.items() if HEADER_ALLOW.match(k)}

    def get(self, provider: str, url: str, headers: dict[str, str]) -> tuple[httpx.Response, Any]:
        response = self.client.get(url, headers=headers)
        return response, self.body_of(response)

    def chat(
        self,
        provider: str,
        base: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        generation=True,
    ) -> dict[str, Any]:
        self.pace(provider, generation)
        started = time.monotonic()
        result: dict[str, Any] = {
            "request": {
                "path": "/chat/completions",
                "fields": sorted(payload),
                "model": payload.get("model"),
                "stream": bool(payload.get("stream")),
            }
        }
        with self.client.stream(
            "POST", base + "/chat/completions", headers=headers, json=payload
        ) as response:
            raw = response.read()
        result["status"] = response.status_code
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        result["headers"] = self.kept_headers(response)
        if payload.get("stream") and response.status_code < 400:
            text = raw.decode("utf-8", "replace")
            result["framing"] = "crlf" if "\r\n\r\n" in text else "lf"
            events = [e for e in re.split(r"\r?\n\r?\n", text) if e.strip()]
            parsed, done = [], False
            for event in events:
                data = "\n".join(
                    line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                )
                other = [
                    line for line in event.splitlines() if line and not line.startswith("data:")
                ]
                if data == "[DONE]":
                    done = True
                    parsed.append("[DONE]")
                    continue
                try:
                    parsed.append(
                        {
                            "data": self.scrub(json.loads(data)),
                            **({"lines": other} if other else {}),
                        }
                    )
                except ValueError:
                    parsed.append({"raw": self.scrub(event[:500])})
            result["events"] = parsed
            result["done_marker"] = done
            usage_events = [
                i
                for i, e in enumerate(parsed)
                if isinstance(e, dict) and (e.get("data") or {}).get("usage")
            ]
            result["usage_event_indexes"] = usage_events
            result["usage_event_choices"] = [
                len(parsed[i]["data"].get("choices") or []) for i in usage_events
            ]
            result["finish_reasons"] = [
                c.get("finish_reason")
                for e in parsed
                if isinstance(e, dict)
                for c in (e.get("data") or {}).get("choices") or []
                if c.get("finish_reason")
            ]
        else:
            try:
                result["body"] = self.scrub(json.loads(raw))
            except ValueError:
                result["body"] = self.scrub(raw.decode("utf-8", "replace")[:4000])
        return result

    # -- model choice --------------------------------------------------------------------
    @staticmethod
    def openai_model(ids: list[str]) -> str | None:
        for preferred in (
            "gpt-4.1-nano",
            "gpt-4o-mini",
            "gpt-5-nano",
            "gpt-4.1-mini",
            "gpt-5-mini",
        ):
            if preferred in ids:
                return preferred
        skip = re.compile(r"audio|realtime|search|tts|transcribe|image|codex|instruct")
        return next(
            (
                i
                for i in sorted(ids)
                if re.match(r"^gpt-[\w.]+-(nano|mini)$", i) and not skip.search(i)
            ),
            None,
        )

    @staticmethod
    def gemini_model(ids: list[str]) -> str | None:
        bare = [i.removeprefix("models/") for i in ids]
        skip = re.compile(
            r"image|tts|live|audio|embedding|veo|imagen|exp|thinking|robotics|computer"
        )

        def version(name: str) -> float:
            match = re.match(r"gemini-(\d+(?:\.\d+)?)", name)
            return float(match.group(1)) if match else 0.0

        for family in ("flash-lite", "flash"):
            for allow_preview in (False, True):
                pool = [
                    i
                    for i in bare
                    if i.startswith("gemini-")
                    and family in i
                    and (family != "flash" or "flash-lite" not in i)
                    and not skip.search(i)
                    and (allow_preview or "preview" not in i)
                ]
                if pool:
                    return max(pool, key=lambda i: (version(i), -len(i)))
        return None

    # -- providers -----------------------------------------------------------------------
    def probe_openai(self) -> None:
        secret = self.keys.get("openai")
        info: dict[str, Any] = self.summary["providers"].setdefault("openai", {})
        if not secret:
            info["skipped"] = "no OPENAI_API_KEY"
            return
        auth = self.headers_for("bearer", secret)
        response, body = self.get("openai", OPENAI + "/models", auth)
        ids = (
            sorted(m.get("id") for m in (response.json().get("data") or []) if isinstance(m, dict))
            if response.status_code == 200
            else []
        )
        self.write(
            "openai",
            "models",
            {
                "status": response.status_code,
                "headers": self.kept_headers(response),
                "model_count": len(ids),
                "sample_ids": [i for i in ids if i.startswith(("gpt-", "o"))][:40],
                **({} if response.status_code == 200 else {"body": body}),
            },
        )
        info["models_status"] = response.status_code
        info["model_count"] = len(ids)
        bogus = self.chat(
            "openai",
            OPENAI,
            self.headers_for("bearer", "sk-invalid-probe"),
            {"model": "gpt-4.1-nano", "messages": PROMPT, "max_completion_tokens": 16},
            generation=False,
        )
        self.write("openai", "invalid_key", bogus)
        info["invalid_key_status"] = bogus["status"]
        if response.status_code != 200:
            info["skipped"] = "model listing failed; generation skipped"
            return
        model = self.openai_model(ids)
        info["chosen_model"] = model
        missing = self.chat(
            "openai",
            OPENAI,
            auth,
            {
                "model": "quotamesh-probe-no-such-model",
                "messages": PROMPT,
                "max_completion_tokens": 16,
            },
            generation=False,
        )
        self.write("openai", "unknown_model", missing)
        info["unknown_model_status"] = missing["status"]
        if "gpt-5-nano" in ids:
            rejected = self.chat(
                "openai",
                OPENAI,
                auth,
                {"model": "gpt-5-nano", "messages": PROMPT, "max_tokens": 16},
                generation=False,
            )
            self.write("openai", "gpt5_max_tokens", rejected)
            info["gpt5_max_tokens_status"] = rejected["status"]
        if not model:
            info["skipped"] = "no inexpensive chat model found"
            return
        extra = {"reasoning_effort": "minimal"} if model.startswith("gpt-5") else {}
        cap = {"max_completion_tokens": 64 if extra else 16}
        cases = {
            "nonstream": {"model": model, "messages": PROMPT, **cap, **extra},
            "stream_plain": {"model": model, "messages": PROMPT, "stream": True, **cap, **extra},
            "stream_usage": {
                "model": model,
                "messages": PROMPT,
                "stream": True,
                "stream_options": {"include_usage": True},
                **cap,
                **extra,
            },
            "tool_stream": {
                "model": model,
                "messages": [{"role": "user", "content": "What is the status of order A-17?"}],
                "tools": TOOLS,
                "tool_choice": "required",
                "stream": True,
                "max_completion_tokens": 64,
                **extra,
            },
            "json_mode": {
                "model": model,
                "messages": [{"role": "user", "content": 'Return the JSON object {"ok": true}.'}],
                "response_format": {"type": "json_object"},
                **cap,
                **extra,
            },
        }
        self.run_cases("openai", OPENAI, auth, cases, info)

    def probe_gemini(self) -> None:
        secret = self.keys.get("gemini")
        info: dict[str, Any] = self.summary["providers"].setdefault("gemini", {})
        if not secret:
            info["skipped"] = "no GEMINI_API_KEY"
            return
        auth_results = {}
        listing = None
        for style in ("bearer", "x-goog-api-key"):
            response, body = self.get("gemini", GEMINI + "/models", self.headers_for(style, secret))
            auth_results["compat_" + style] = response.status_code
            if response.status_code == 200 and listing is None:
                listing = (style, response)
            else:
                self.write(
                    "gemini",
                    f"models_auth_{style.replace('-', '_')}",
                    {
                        "status": response.status_code,
                        "headers": self.kept_headers(response),
                        "body": body,
                    },
                )
        native, native_body = self.get(
            "gemini",
            GEMINI_NATIVE + "/models?pageSize=5",
            self.headers_for("x-goog-api-key", secret),
        )
        auth_results["native_x-goog-api-key"] = native.status_code
        if native.status_code != 200:
            self.write(
                "gemini",
                "native_models_auth",
                {
                    "status": native.status_code,
                    "headers": self.kept_headers(native),
                    "body": native_body,
                },
            )
        info["auth"] = auth_results
        for style in ("bearer", "x-goog-api-key"):
            bogus = self.chat(
                "gemini",
                GEMINI,
                self.headers_for(style, "quotamesh-probe-invalid-gemini-key"),
                {"model": "gemini-2.5-flash-lite", "messages": PROMPT, "max_completion_tokens": 16},
                generation=False,
            )
            self.write("gemini", f"invalid_key_{style.replace('-', '_')}", bogus)
            info[f"invalid_key_status_{style}"] = bogus["status"]
        if listing is None:
            info["skipped"] = "key rejected by every auth style; generation skipped"
            return
        style, response = listing
        info["auth_style"] = style
        ids = sorted(m.get("id") for m in response.json().get("data") or [] if isinstance(m, dict))
        self.write(
            "gemini",
            "models",
            {
                "status": response.status_code,
                "headers": self.kept_headers(response),
                "auth_style": style,
                "model_count": len(ids),
                "id_prefixes": sorted({i.split("/", 1)[0] + "/" for i in ids if "/" in i}),
                "sample_ids": ids[:60],
            },
        )
        model = self.gemini_model(ids)
        info["chosen_model"] = model
        info["model_count"] = len(ids)
        auth = self.headers_for(style, secret)
        missing = self.chat(
            "gemini",
            GEMINI,
            auth,
            {
                "model": "gemini-quotamesh-probe-no-such-model",
                "messages": PROMPT,
                "max_completion_tokens": 16,
            },
            generation=False,
        )
        self.write("gemini", "unknown_model", missing)
        info["unknown_model_status"] = missing["status"]
        if not model:
            info["skipped"] = "no flash/flash-lite model found"
            return
        cases = {
            "nonstream": {"model": model, "messages": PROMPT, "max_completion_tokens": 32},
            "stream_plain": {"model": model, "messages": PROMPT, "stream": True, "max_tokens": 32},
            "stream_usage": {
                "model": model,
                "messages": PROMPT,
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_completion_tokens": 32,
            },
            "models_prefix": {
                "model": "models/" + model,
                "messages": PROMPT,
                "max_completion_tokens": 32,
            },
            "tool_stream": {
                "model": model,
                "messages": [{"role": "user", "content": "What is the status of order A-17?"}],
                "tools": TOOLS,
                "tool_choice": "required",
                "stream": True,
                "max_completion_tokens": 128,
            },
            "json_mode": {
                "model": model,
                "messages": [{"role": "user", "content": 'Return the JSON object {"ok": true}.'}],
                "response_format": {"type": "json_object"},
                "max_completion_tokens": 64,
            },
        }
        self.run_cases("gemini", GEMINI, auth, cases, info)

    def run_cases(self, provider, base, auth, cases, info) -> None:
        for case, payload in cases.items():
            try:
                result = self.chat(provider, base, auth, payload)
            except Budget as exc:
                info.setdefault("not_run", []).append(case)
                print(f"  {provider}/{case}: skipped ({exc})")
                continue
            except httpx.HTTPError as exc:
                info.setdefault("errors", {})[case] = type(exc).__name__
                print(f"  {provider}/{case}: {type(exc).__name__}")
                continue
            self.write(provider, case, result)
            line = {"status": result["status"], "latency_ms": result["latency_ms"]}
            if "events" in result:
                line.update(
                    done_marker=result["done_marker"],
                    framing=result["framing"],
                    usage_event_indexes=result["usage_event_indexes"],
                    usage_event_choices=result["usage_event_choices"],
                    event_count=len(result["events"]),
                    finish_reasons=result["finish_reasons"],
                )
            elif isinstance(result.get("body"), dict):
                usage = result["body"].get("usage")
                if usage:
                    line["usage"] = usage
                choices = result["body"].get("choices") or []
                if choices:
                    line["finish_reason"] = choices[0].get("finish_reason")
                if result["status"] >= 400:
                    line["error"] = result["body"]
            info.setdefault("cases", {})[case] = line
            print(f"  {provider}/{case}: HTTP {result['status']} in {result['latency_ms']} ms")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/providers"))
    parser.add_argument("--max-calls", type=int, default=16)
    parser.add_argument("--only", choices=["openai", "gemini"])
    args = parser.parse_args()

    parsed = parse_env(args.env_file.read_bytes())
    keys = {}
    for provider, names in {
        "openai": ("OPENAI_API_KEY",),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    }.items():
        entry = next((parsed.entries[n] for n in names if n in parsed.entries), None)
        if entry and entry.value.reveal().strip():
            keys[provider] = entry.value.reveal().strip()
    probe = Probe(keys, args.out, args.max_calls)
    try:
        for provider in ("gemini", "openai"):
            if args.only and provider != args.only:
                continue
            print(f"Probing {provider}…")
            getattr(probe, "probe_" + provider)()
    finally:
        probe.summary["generation_calls"] = probe.generation_calls
        text = json.dumps(probe.summary, indent=2, ensure_ascii=False) + "\n"
        probe.assert_clean(text)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "summary.json").write_text(text, encoding="utf-8", newline="\n")
        probe.client.close()
    print(f"Generation calls used: {probe.generation_calls}/{args.max_calls}")
    print(json.dumps(probe.summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
