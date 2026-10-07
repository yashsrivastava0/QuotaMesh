# QuotaMesh agent guide

## Project context

QuotaMesh is a local AI API capacity gateway. The product reference is [`QuotaMesh_MVP_Architecture_Product_Spec_v0.3.pdf`](QuotaMesh_MVP_Architecture_Product_Spec_v0.3.pdf); the implementation sequence and boundaries are in [`ROADMAP.md`](ROADMAP.md). The current implementation includes Phase 1 secure pass-through, Phase 2 deterministic routing, ordered pools, scoped cooldowns, expiry, and local paid caps, Phase 3 Capacity Wallet, named profiles, credential lifecycle and durable usage/history, and Phase 4 Explain/Connect, manual Doctor, integration snippets, environment references, local activity and a dated Access Catalog. Keep later phase features out of scope unless requested or a minimal prerequisite is necessary and documented.

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
- `src/quotamesh/config.py` and `registry/`: local paths, input validation, and dated provider endpoints.
- `src/quotamesh/ui/`: Jinja templates and static styles.
- `src/quotamesh/cli.py` and `demo.py`: user commands and fake upstream for local smoke checks.
- `tests/`: gateway behavior and security tests.
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

## Local commands

```sh
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/python -m build
```

Python 3.11 or newer is required. See [`README.md`](README.md) for setup and safe fake-upstream usage. Never commit local data, secrets, virtual environments, build output, `.codex/` settings, or `.DS_Store` files.
