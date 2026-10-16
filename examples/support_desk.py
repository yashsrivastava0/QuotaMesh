"""Acme Support Desk: tool calling and JSON triage through QuotaMesh.

(a) Tool calling: the model asks for lookup_order(order_id), a local Python function.
    The assistant message is sent back exactly as received, including provider extras
    such as Gemini's extra_content.google.thought_signature, which Gemini requires on the
    next turn. Tool turns are non-streaming, and tool calls are read whatever the
    finish_reason says (Gemini reports "stop" rather than "tool_calls").
(b) JSON triage: response_format {"type": "json_object"} turns a ticket into
    {"category", "priority", "summary"}, which is parsed and validated.

A 429 is retried once after its Retry-After; a 503 explains that no capacity is eligible.

    pip install openai
    # PowerShell: $env:QUOTAMESH_API_KEY = (quotamesh key | Out-String).Trim()
    # bash:       export QUOTAMESH_API_KEY="$(quotamesh key)"
    python examples/support_desk.py
    python examples/support_desk.py --profile paid-backup --max-tokens 300 --only triage
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from local_key import KEY_HELP, local_gateway_key

try:
    import openai
except ImportError:
    sys.exit("This example needs the OpenAI SDK: pip install openai")

DEFAULT_BASE_URL = "http://127.0.0.1:8787/v1"
ORDERS = {
    "A-1042": {"status": "shipped", "carrier": "Acme Express", "eta": "Thursday"},
    "A-17": {"status": "processing", "carrier": None, "eta": "next week"},
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": "Look up the status of an Acme order by its id, such as A-1042.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "Order id"}},
                "required": ["order_id"],
            },
        },
    }
]
AGENT = (
    "You are Acme's support assistant. Use the lookup_order tool for any order question, "
    "then answer the customer in one or two friendly sentences."
)
QUESTION = "Hi! Where is my order A-1042?"
CATEGORIES = {"billing", "shipping", "technical", "account", "other"}
PRIORITIES = {"low", "medium", "high", "urgent"}
TRIAGE = (
    "You triage Acme support tickets. Reply with only a JSON object with the keys "
    '"category" (one of billing, shipping, technical, account, other), "priority" '
    '(one of low, medium, high, urgent) and "summary" (one sentence, at most 20 words).'
)
TICKET = (
    "I was charged twice for invoice 2291 this month. Please refund the duplicate "
    "payment before Friday, when my rent is due."
)


def lookup_order(order_id: str) -> dict:
    """The local tool. Real code would query an order database."""
    order = ORDERS.get(order_id.strip().upper())
    return {"order_id": order_id, **order} if order else {"order_id": order_id, "error": "unknown"}


def server_message(exc: openai.APIStatusError) -> str:
    body = exc.body
    if isinstance(body, list) and body and isinstance(body[0], dict):  # Gemini error shape
        body = body[0].get("error", body[0])
    return str(body.get("message") or exc.message) if isinstance(body, dict) else exc.message


def retry_after(exc: openai.APIStatusError) -> float:
    try:
        return min(60.0, max(1.0, float(exc.response.headers.get("retry-after", "5"))))
    except ValueError:  # an HTTP-date; wait a short default instead of parsing it
        return 5.0


class Desk:
    def __init__(self, client: openai.OpenAI, model: str, max_tokens: int):
        self.client, self.model, self.max_tokens = client, model, max_tokens

    def create(self, **params) -> dict:
        """One non-streaming call returning the raw JSON body; a 429 is retried once."""
        for attempt in (1, 2):
            try:
                raw = self.client.chat.completions.with_raw_response.create(
                    model=self.model, max_completion_tokens=self.max_tokens, **params
                )
            except openai.RateLimitError as exc:
                if attempt == 2:
                    raise
                wait = retry_after(exc)
                print(f"  rate limited ({server_message(exc)}); retrying once in {wait:.0f} s")
                time.sleep(wait)
                continue
            headers = raw.headers
            print(
                f"  [served by {headers.get('x-quotamesh-provider')}/"
                f"{headers.get('x-quotamesh-model')}, attempts "
                f"{headers.get('x-quotamesh-attempts')}]"
            )
            # The raw body keeps every field, including ones the SDK types do not know.
            return raw.http_response.json()
        raise AssertionError("unreachable")

    def answer_with_tools(self, question: str) -> str:
        messages: list[dict] = [
            {"role": "system", "content": AGENT},
            {"role": "user", "content": question},
        ]
        for turn in range(1, 4):
            body = self.create(
                messages=messages, tools=TOOLS, tool_choice="required" if turn == 1 else "auto"
            )
            message = body["choices"][0]["message"]
            # Read tool calls regardless of finish_reason, and never rely on an "index".
            calls = message.get("tool_calls") or []
            if not calls:
                if turn == 1:
                    print("  (the model answered without calling lookup_order)")
                return message.get("content") or ""
            messages.append(message)  # echoed exactly as received, signature included
            for number, call in enumerate(calls):
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                if function.get("name") == "lookup_order":
                    result = lookup_order(str(arguments.get("order_id", "")))
                else:
                    result = {"error": "unknown tool"}
                print(f"  tool {function.get('name')}({json.dumps(arguments)}) -> {result}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{number}",
                        "content": json.dumps(result),
                    }
                )
        return "(no final answer after three tool rounds)"

    def triage(self, ticket: str) -> tuple[dict | None, str]:
        body = self.create(
            messages=[{"role": "system", "content": TRIAGE}, {"role": "user", "content": ticket}],
            response_format={"type": "json_object"},
        )
        text = (body["choices"][0]["message"].get("content") or "").strip()
        if text.startswith("```"):  # tolerate a fenced reply
            text = text.strip("`").removeprefix("json").strip()
        try:
            result = json.loads(text)
        except ValueError:
            return None, "the reply was not JSON"
        if not isinstance(result, dict):
            return None, "the reply was not a JSON object"
        if result.get("category") not in CATEGORIES:
            return None, "category is missing or not one of the allowed values"
        if result.get("priority") not in PRIORITIES:
            return None, "priority is missing or not one of the allowed values"
        if not isinstance(result.get("summary"), str) or not result["summary"].strip():
            return None, "summary is missing"
        return result, ""


def describe(exc: openai.APIStatusError, model: str) -> str:
    message, code = server_message(exc), exc.code or "error"
    if exc.status_code == 503:
        return (
            f"No capacity is eligible for {model} right now ({message}). Open Route & test in "
            "the dashboard to see why each target was skipped, or add a backup target."
        )
    if exc.status_code == 429:
        return f"Still rate limited after one retry ({message}). Try again later."
    if exc.status_code == 504:
        return f"No provider answered before the route deadline ({message}). Try again later."
    if exc.status_code == 403 and code == "paid_blocked":
        return f"{model} does not allow paid access ({message}). Enable it in Profiles, with a cap."
    if exc.status_code == 401:
        return "QuotaMesh rejected the local key. Set QUOTAMESH_API_KEY from `quotamesh key`."
    return f"QuotaMesh returned HTTP {exc.status_code} ({code}): {message}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Acme Support Desk through QuotaMesh.")
    parser.add_argument("--profile", help="profile slug to use (model qm/<slug>)")
    parser.add_argument(
        "--max-tokens", type=int, default=300, help="reply cap, sent as max_completion_tokens"
    )
    parser.add_argument("--only", choices=["tools", "triage"], help="run one part only")
    parser.add_argument(
        "--base-url", default=os.environ.get("QUOTAMESH_BASE_URL", DEFAULT_BASE_URL)
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):  # keep piped output in order with stderr
        sys.stdout.reconfigure(errors="replace", line_buffering=True)
    api_key, source = local_gateway_key()
    if not api_key:
        print(KEY_HELP, file=sys.stderr)
        return 2
    if not source.startswith("QUOTAMESH"):
        print(f"Using the local gateway key saved by QuotaMesh ({source}).", file=sys.stderr)
    model = "qm/" + args.profile if args.profile else os.environ.get("QUOTAMESH_MODEL")
    model = model or "qm/default"
    client = openai.OpenAI(base_url=args.base_url, api_key=api_key, max_retries=0, timeout=180)
    desk = Desk(client, model, args.max_tokens)
    succeeded = True
    try:
        if args.only in (None, "tools"):
            print(f"Customer: {QUESTION}")
            print(f"Acme ({model}): {desk.answer_with_tools(QUESTION)}\n")
        if args.only in (None, "triage"):
            print(f"Ticket: {TICKET}")
            result, problem = desk.triage(TICKET)
            if result is None:
                print(f"Triage failed: {problem}.", file=sys.stderr)
                succeeded = False
            else:
                print(f"Triage: {result['category']} / {result['priority']} - {result['summary']}")
    except openai.APIStatusError as exc:
        print(describe(exc, model), file=sys.stderr)
        return 1
    except openai.APIConnectionError:
        print(f"Cannot reach QuotaMesh at {args.base_url}. Is it running?", file=sys.stderr)
        return 1
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
