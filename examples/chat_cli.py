"""Acme terminal chat: a small "user project" that talks to QuotaMesh like any OpenAI app.

It streams replies with the OpenAI Python SDK, then prints which provider and model
QuotaMesh chose, how many upstream attempts that took, and the token usage.

    pip install openai
    python examples/chat_cli.py
    python examples/chat_cli.py --once "Say hello in five words"

On the machine running QuotaMesh the local gateway key is found automatically; elsewhere set
QUOTAMESH_API_KEY to the output of `quotamesh key`.

Settings: QUOTAMESH_BASE_URL (default http://127.0.0.1:8787/v1), QUOTAMESH_API_KEY (the
local gateway key; QUOTAMESH_KEY from the Connect page also works) and QUOTAMESH_MODEL
(default qm/default). Commands: /profile <slug>, /usage, /clear, /help, /quit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

from local_key import KEY_HELP, local_gateway_key

try:
    import openai
except ImportError:
    sys.exit("This example needs the OpenAI SDK: pip install openai")

DEFAULT_BASE_URL = "http://127.0.0.1:8787/v1"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
HELP = (
    "Commands: /profile <slug> switches to qm/<slug>, /usage shows today's gateway usage, "
    "/clear forgets the conversation, /quit exits."
)
def server_message(exc: openai.APIStatusError) -> str:
    """The error text QuotaMesh (or the provider behind it) returned."""
    body = exc.body
    if isinstance(body, list) and body and isinstance(body[0], dict):  # Gemini error shape
        body = body[0].get("error", body[0])
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return exc.message


def skipped_targets(exc: openai.APIStatusError) -> str:
    """QuotaMesh lists every candidate it could not use and why."""
    try:
        candidates = exc.response.json().get("quotamesh", {}).get("candidates", [])
    except (ValueError, AttributeError):
        return ""
    reasons = [
        f"#{c.get('position')} {c.get('provider_id')}/{c.get('model')}: {c.get('skip_reason')}"
        for c in candidates
        if isinstance(c, dict) and not c.get("eligible")
    ]
    return " Skipped: " + "; ".join(reasons) + "." if reasons else ""


def explain(exc: openai.APIStatusError, model: str) -> str:
    message, code, status = server_message(exc), exc.code or "", exc.status_code
    if status == 401:
        return f"QuotaMesh rejected the local key ({message}).\n{KEY_HELP}"
    if status == 403 and code == "paid_blocked":
        return (
            f"{model} may not use paid or unknown-plan access ({message}). Allow paid use for "
            "this profile in the dashboard (Profiles), preferably with a daily cap."
        )
    if status == 404 and code == "profile_not_found":
        return f"There is no saved profile {model} ({message}). Try /profile <saved slug>."
    if status == 429:
        retry = exc.response.headers.get("retry-after")
        wait = f" Retry after {retry} s." if retry else ""
        if code == "no_candidates":  # QuotaMesh's own answer: every target is cooling down
            return f"All targets for {model} are cooling down ({message}).{wait}"
        return f"The provider rate limited this request ({message}).{wait}"
    if status == 503 and code == "not_configured":
        return f"QuotaMesh has no default profile yet ({message}). Finish setup in the dashboard."
    if status == 503 and code == "no_candidates":
        return f"No eligible capacity for {model}: {message}.{skipped_targets(exc)}"
    if status == 503:
        return f"The provider is unavailable right now ({message}). Retry, or add a backup target."
    if status == 504:
        return (
            f"No provider answered before the route deadline ({message}). Slow non-stream models "
            "can take 15-30 s; streaming or a backup target helps."
        )
    return f"QuotaMesh returned HTTP {status} ({code or 'error'}): {message}"


def route_footer(headers, usage) -> str:
    """One line from the X-QuotaMesh-* response headers and the stream's usage chunk."""
    provider = headers.get("x-quotamesh-provider", "?")
    model = headers.get("x-quotamesh-model", "?")
    attempts = headers.get("x-quotamesh-attempts", "?")
    fallback = "yes" if headers.get("x-quotamesh-fallback") == "true" else "no"
    tokens = f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage else "?/?"
    return (
        f"  served by {provider}/{model} · attempts {attempts} · fallback {fallback}"
        f" · tokens in/out {tokens}"
    )


class Chat:
    def __init__(self, base_url: str, api_key: str, model: str, max_tokens: int):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        # Plain role/content history stays portable when QuotaMesh falls back to another
        # provider. Provider extras (such as Gemini thought signatures) matter for tool-call
        # turns; support_desk.py shows how to echo those unchanged.
        self.history: list[dict[str, str]] = []
        # QuotaMesh owns retries and fallback, so the SDK must not retry on its own.
        self.client = openai.OpenAI(
            base_url=self.base_url, api_key=api_key, max_retries=0, timeout=180
        )

    def ask(self, prompt: str, prefix: str = "") -> bool:
        self.history.append({"role": "user", "content": prompt})
        try:
            reply, footer = self.stream(prefix)
        except openai.APIStatusError as exc:
            problem = explain(exc, self.model)
        except openai.APIConnectionError:
            problem = f"Cannot reach QuotaMesh at {self.base_url}. Is `quotamesh start` running?"
        except openai.APIError as exc:  # an SSE error event after the reply had started
            problem = f"\n[stream interrupted: {exc.message}]"
        except KeyboardInterrupt:  # closing the stream also stops the provider request
            problem = "\n[cancelled]"
        else:
            self.history.append({"role": "assistant", "content": reply})
            print(footer)
            return True
        self.history.pop()
        print(problem, file=sys.stderr)
        return False

    def stream(self, prefix: str) -> tuple[str, str]:
        reply: list[str] = []
        usage = None
        with self.client.chat.completions.with_streaming_response.create(
            model=self.model,
            messages=self.history,
            stream=True,
            # Ask for usage explicitly; QuotaMesh passes the provider's usage through.
            stream_options={"include_usage": True},
            max_completion_tokens=self.max_tokens,
        ) as response:
            headers = response.headers
            for chunk in response.parse():
                for choice in chunk.choices:  # a usage-only chunk has no choices
                    text = choice.delta.content if choice.delta else None
                    if text:
                        print(prefix + text if not reply else text, end="", flush=True)
                        reply.append(text)
                if chunk.usage:  # Gemini repeats cumulative usage; the last one counts
                    usage = chunk.usage
        print()
        return "".join(reply), route_footer(headers, usage)

    def usage_today(self) -> str:
        """GET /api/wallet with the same bearer key (a QuotaMesh management API)."""
        root = self.base_url.removesuffix("/v1")
        request = urllib.request.Request(
            root + "/api/wallet",
            headers={"Authorization": "Bearer " + self.api_key, "Accept": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only
        try:
            with opener.open(request, timeout=30) as response:
                today = json.load(response).get("today") or {}
        except urllib.error.HTTPError as exc:
            return f"Usage is unavailable (HTTP {exc.code})."
        except (OSError, ValueError):
            return f"Cannot reach QuotaMesh at {root}."

        def tokens(value):
            return "unknown" if value is None else str(value)

        line = (
            f"Today (UTC, traffic through QuotaMesh only): {today.get('routed_requests', 0)} "
            f"requests, {today.get('attempts', 0)} upstream attempts, tokens in/out "
            f"{tokens(today.get('input_tokens'))}/{tokens(today.get('output_tokens'))}, "
            f"known cost ${today.get('known_cost_usd') or 0:.6f}"
        )
        if today.get("unknown_cost"):
            line += f" (+{today['unknown_cost']} attempt(s) with unknown cost)"
        return line


def command(chat: Chat, line: str) -> bool:
    """Handle a /command; return False to quit."""
    name, _, argument = line.partition(" ")
    if name in {"/quit", "/exit"}:
        return False
    if name == "/clear":
        chat.history.clear()
        print("Conversation cleared.")
    elif name == "/profile":
        slug = argument.strip().removeprefix("qm/")
        if SLUG.fullmatch(slug):
            chat.model = "qm/" + slug
            print(f"Now using {chat.model}.")
        else:
            print("Usage: /profile <slug> (lowercase letters, digits and single hyphens)")
    elif name == "/usage":
        print(chat.usage_today())
    else:
        print(HELP)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Chat through QuotaMesh.")
    parser.add_argument("--once", metavar="PROMPT", help="send one prompt, print the reply, exit")
    parser.add_argument("--profile", help="profile slug to use (model qm/<slug>)")
    parser.add_argument(
        "--max-tokens", type=int, default=512, help="reply cap, sent as max_completion_tokens"
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("QUOTAMESH_BASE_URL", DEFAULT_BASE_URL)
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):  # model output may not fit a legacy console codepage
        sys.stdout.reconfigure(errors="replace", line_buffering=True)
    api_key, source = local_gateway_key()
    if not api_key:
        print(KEY_HELP, file=sys.stderr)
        return 2
    if not source.startswith("QUOTAMESH"):
        print(f"Using the local gateway key saved by QuotaMesh ({source}).", file=sys.stderr)
    model = "qm/" + args.profile if args.profile else os.environ.get("QUOTAMESH_MODEL")
    chat = Chat(args.base_url, api_key, model or "qm/default", args.max_tokens)
    if args.once is not None:
        return 0 if chat.ask(args.once) else 1
    print(f"Chatting with {chat.model} through {chat.base_url}. {HELP}")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.startswith("/"):
            if not command(chat, line):
                return 0
        elif line:
            chat.ask(line, prefix="assistant> ")


if __name__ == "__main__":
    sys.exit(main())
