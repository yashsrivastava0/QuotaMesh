# Examples: QuotaMesh from a user's project

These scripts are ordinary OpenAI SDK applications. They never see a provider key and
never pick a provider. They call one local base URL with the local gateway key and a
profile alias (`qm/<slug>`); QuotaMesh chooses the target, falls back, applies the paid
rules and reports what happened in `X-QuotaMesh-*` response headers.

| Script | What it shows |
| --- | --- |
| [`chat_cli.py`](chat_cli.py) | Multi-turn streaming chat. After each reply: `served by <provider>/<model> · attempts N · fallback yes/no · tokens in/out`. Commands `/profile <slug>`, `/usage`, `/clear`, `/quit`; `--once "prompt"` for scripts. Clear messages for 401, 403 `paid_blocked`, 404 `profile_not_found`, 429 (with Retry-After), 503 no capacity and 504 deadline. |
| [`support_desk.py`](support_desk.py) | "Acme Support Desk": a tool call to a local `lookup_order()` function with the assistant message echoed back unchanged (Gemini's thought signature included), then JSON-mode ticket triage into `category`, `priority` and `summary`. Retries a 429 once after Retry-After; explains a 503. `--profile`, `--max-tokens`, `--only tools\|triage`. |

## Install and configure

```sh
pip install openai            # or, from a checkout: pip install -e ".[examples]"
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `QUOTAMESH_BASE_URL` | `http://127.0.0.1:8787/v1` | The gateway (the demo uses port 8788) |
| `QUOTAMESH_API_KEY` | the key saved in QuotaMesh's data directory | The **local gateway key** printed by `quotamesh key`; `QUOTAMESH_KEY` (set by the Connect page) also works. It is not a provider key. |
| `QUOTAMESH_MODEL` | `qm/default` | The profile alias; `--profile <slug>` overrides it |

`--max-tokens` caps each reply and is sent as `max_completion_tokens`, which OpenAI and
Gemini both accept (GPT-5 models reject `max_tokens`).

## Try them without provider keys (`quotamesh demo`)

The demo runs a fake provider on port **8788** with temporary data and simulated dollars.
It prints a `quotamesh key --data-dir "<temporary directory>"` command for its own key.

PowerShell:

```powershell
quotamesh demo --no-browser            # terminal 1; note the printed --data-dir path
$env:QUOTAMESH_BASE_URL = "http://127.0.0.1:8788/v1"
$env:QUOTAMESH_API_KEY = (quotamesh key --data-dir "<printed directory>" | Out-String).Trim()
python examples/chat_cli.py --once "Hello"
python examples/chat_cli.py
python examples/support_desk.py
```

bash:

```sh
quotamesh demo --no-browser            # terminal 1; note the printed --data-dir path
export QUOTAMESH_BASE_URL="http://127.0.0.1:8788/v1"
export QUOTAMESH_API_KEY="$(quotamesh key --data-dir '<printed directory>')"
python examples/chat_cli.py --once "Hello"
```

What to expect from the demo's starting scenario:

- The first request shows `attempts 2 · fallback yes`: the primary fake key is rate
  limited, the free backup answers. A second request within five seconds shows
  `attempts 1`, because the cooling primary is skipped instead of retried.
- `/profile paid-backup` routes to a simulated trial source and `/usage` then shows the
  simulated $0.60 cost; `/profile free-app` stays on free access only.
- `/profile no-such-profile` shows the 404 `profile_not_found` message.
- The fake provider always answers "Hello from fake upstream" and ignores tools and JSON
  mode, so `support_desk.py` reports that the model answered without calling the tool and
  that triage did not return JSON (exit code 1). That is the expected error handling;
  use real keys for the full flow.
- Scenarios from the dashboard (or `POST /api/demo`) exercise other paths: `paid-guard`
  ends in a 429, `midstream` prints `[stream interrupted: ...]`.

## Run them against your own keys

Start QuotaMesh, add your keys (paste a `.env` or add a key in the dashboard), and save a
default profile, as described in the [README](../README.md#quick-start). With QuotaMesh
running, from the repository folder:

```powershell
.venv\Scripts\python.exe examples\chat_cli.py
.venv\Scripts\python.exe examples\chat_cli.py --profile with-backup --once "Say hello in five words"
.venv\Scripts\python.exe examples\support_desk.py --max-tokens 300
```

```sh
.venv/bin/python examples/chat_cli.py --profile my-app
```

On the machine where QuotaMesh runs, the examples find the local gateway key by themselves
(the file `quotamesh key` prints, in QuotaMesh's data directory). Set `QUOTAMESH_API_KEY`
only when the examples run elsewhere or should use another data directory.

Each chat turn is one routed request; the support desk makes three (two tool turns and one
triage) when the model calls the tool once. Paid providers are used only when the profile
allows paid access, and then within its caps. Use `/usage` or the dashboard's Activity page
to see the counts and the known cost.

## Provider differences the examples handle

- **Gemini tool calls** arrive with `finish_reason: "stop"` (not `"tool_calls"`), streamed
  tool calls have no `index`, and Gemini 3 adds `extra_content.google.thought_signature`.
  `support_desk.py` uses non-streaming tool turns, reads `tool_calls` whatever the finish
  reason, and appends the raw assistant message unchanged, so the signature goes back.
- **Gemini non-stream latency** was 15-30 s in our live checks; `chat_cli.py` streams.
- **Usage in streams**: the examples request `stream_options.include_usage`; OpenAI then
  sends a final chunk with empty `choices`, Gemini repeats cumulative usage on content
  chunks. Both are handled.
- `chat_cli.py` keeps plain role/content history so a conversation stays portable if a
  profile falls back to another provider. A tool conversation echoes provider-specific
  fields, so keep it on targets of one provider, or restart it after a cross-provider
  fallback, because another provider may reject those fields.

See [live provider notes](../docs/live-provider-notes.md) for the evidence behind these.
