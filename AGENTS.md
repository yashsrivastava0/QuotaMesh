# QuotaMesh agent guide

## Project context

QuotaMesh is a local AI API capacity gateway. The product reference is [`QuotaMesh_MVP_Architecture_Product_Spec_v0.3.pdf`](QuotaMesh_MVP_Architecture_Product_Spec_v0.3.pdf); the implementation sequence and boundaries are in [`ROADMAP.md`](ROADMAP.md). The current implementation includes Phase 1 secure pass-through, Phase 2 deterministic routing, ordered pools, scoped cooldowns, expiry, and local paid caps, Phase 3 Capacity Wallet, named profiles, credential lifecycle and durable usage/history, and Phase 4 Explain/Connect, manual Doctor, integration snippets, environment references, local activity and a dated Access Catalog. Phase 5 finalizes the MVP release, setup, passive observations, packaging and platform QA. No Phase 6 is planned. Deferred PDF extensions remain outside the finalized MVP unless explicitly requested. Post-MVP live-provider readiness (real OpenAI and Gemini behaviour, recorded in [`docs/live-provider-notes.md`](docs/live-provider-notes.md)) hardens classification, streaming accounting, setup and the dashboard without changing these boundaries.

## Start each task

1. Read this file and any nearer `AGENTS.md` before editing.
2. Inspect `git status --short --branch`, the current branch, recent commits, and the files related to the task. Preserve existing user changes.
3. Use one task branch per task: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, or `chore/<slug>`. Keep `main` as the integration branch. Do not force-push or silently rewrite history.
4. Make small, reviewable commits using Conventional Commit subjects. Include an issue or task reference in the commit body when one exists; otherwise use the task slug in the branch and commit scope. Follow [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Project map

- `src/quotamesh/engine/`: pure selection, classification, quota transitions, and cost metadata.
- `src/quotamesh/routing.py` and `policy.py`: runtime snapshots and validated local policy.
- `src/quotamesh/app.py`: application factory, shared state, process lifecycle, and router wiring.
- `src/quotamesh/routes/wallet.py`, `wallet.py`, `usage.py`, and `credentials.py`: profile/credential management, grouped capacity and honest durable observations.
- `src/quotamesh/routes/dashboard.py`: first-run bootstrap, setup UI, status, and connection management.
- `src/quotamesh/routes/gateway.py`: authenticated Chat Completions proxy and SSE transport.
- `src/quotamesh/connect.py`, `doctor.py`, `activity.py`, and `routes/connect.py`: shared explanation, safe connection templates, explicit bounded diagnostics, environment import and metadata notifications.
- `src/quotamesh/security.py`: local host/origin checks and browser/bearer authorization.
- `src/quotamesh/store.py`: SQLite schema, migrations, credentials, and metadata-only request history.
- `src/quotamesh/config.py` and `registry/`: local paths, input validation, dated provider endpoints and evidence-backed quirk flags (`stream_usage`, `token_param`, `model_list_prefix`, `reset_rule`).
- `src/quotamesh/envfile.py`: safe `.env` parsing for key import; values never print.
- `src/quotamesh/ui/`: Jinja templates and static styles.
- `src/quotamesh/cli.py` and `demo.py`: user commands and fake upstream for local smoke checks.
- `tests/`: gateway behavior and security tests; `tests/conftest.py` blocks real network; `tests/live/` is the opt-in real-provider suite; `tests/fixtures/providers/` holds sanitized real responses.
- `scripts/live_probe.py`, `scripts/bench_gateway.py`: budgeted real-provider evidence capture and the in-process overhead benchmark.
- `examples/`: small applications that use QuotaMesh the way a user's project would.
- `docs/`: architecture and development workflow.

## Engineering constraints

- Keep the service bound to loopback and single-process. Do not expose it on a network interface.
- Never log or persist prompts, completions, provider secrets, or authorization headers. Request history is metadata only.
- Preserve upstream JSON fields. Do not silently normalize unsupported API shapes.
- Keep redirects disabled. Validate custom remote endpoints as HTTPS and allow HTTP only for loopback.
- Paid and unknown-plan use must remain explicitly disabled by default.
- For streaming, validate the first SSE event before returning a successful response; report mid-stream failures as SSE errors.
- Keep provider claims and dates backed by the registry or tests. Treat the PDF as product intent and document any implementation assumption.
- Add third-party dependencies only when needed; update `pyproject.toml` and explain the reason.
- Ordinary tests must never reach a real provider (the autouse guard in `tests/conftest.py`). Real-provider tests are opt-in (`-m live`), read keys only from the git-ignored `.env`, stay within their call budgets and never print or store key values.
- New provider quirks need live evidence or provider documentation: capture a sanitized fixture, record it in `docs/live-provider-notes.md`, then add an explicit registry flag.

## Local commands

```powershell
# Windows (PowerShell)
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m build
```

```sh
# macOS / Linux
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/python -m build
```

Opt-in real-provider checks (cost-bounded; see [`docs/live-testing.md`](docs/live-testing.md)):

```powershell
.venv\Scripts\python.exe -m pytest -m live tests\live -q --tb=short
.venv\Scripts\python.exe scripts\live_probe.py --env-file .env --max-calls 16
```

If the virtual environment has no `pip`, install with `uv pip install --python .venv\Scripts\python.exe -e ".[dev,browser,examples]"`.

Python 3.11 or newer is required. See [`README.md`](README.md) for setup and safe fake-upstream usage. Never commit local data, secrets, virtual environments, build output, `.env` files, `.codex/` or `.claude/` settings, or `.DS_Store` files.
