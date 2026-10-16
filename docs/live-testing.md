# Live provider testing

Ordinary tests never reach a real provider: `tests/conftest.py` refuses every non-loopback DNS
lookup and connection (including the asyncio paths that bypass `socket.connect`). Real-provider
checks are separate, opt-in and budgeted. Run them when you change provider handling, streaming,
accounting or setup, or when a provider changes its API.

## Before you start

1. Put your keys in the git-ignored `.env` at the repository root (see `.env.example`):
   `OPENAI_API_KEY=…`, `GEMINI_API_KEY=…`. Check it is ignored: `git check-ignore -v .env`.
2. Install the extras once (the OpenAI SDK is only needed for the examples and SDK checks):
   - PowerShell: `uv pip install --python .venv\Scripts\python.exe -e ".[dev,examples]"`
   - bash: `uv pip install --python .venv/bin/python -e '.[dev,examples]'`
3. Use a paid OpenAI key only if you accept a few cents of spend at most; Gemini free-tier keys cost
   nothing but are rate limited.

## How keys are protected

- Keys are read only through `quotamesh.envfile`, whose `Secret` values never print.
- The live suite stores **environment references**, not key values, in a temporary database, and
  uses its own temporary data directory and port — never `%LOCALAPPDATA%\QuotaMesh` or port 8787.
- The probe checks every output file for key material before writing it and redacts ids, model
  output, thought signatures and account identifiers.
- Nothing in the request history contains prompts or replies (metadata only).

## 1. Provider probe (direct calls, no QuotaMesh)

Captures how each provider really behaves and refreshes the fixtures in `tests/fixtures/providers/`.

```powershell
.venv\Scripts\python.exe scripts\live_probe.py --env-file .env --out tests\fixtures\providers --max-calls 16
```

Model listings and rejected requests are free; `--max-calls` caps generation calls (about 11 per
full run, each limited to a few output tokens). Review `git diff tests/fixtures/providers` before
committing and record any behaviour change in [`live-provider-notes.md`](live-provider-notes.md).

## 2. Live user-story suite

```powershell
.venv\Scripts\python.exe -m pytest -m live tests\live -q --tb=short -p no:cacheprovider
```

```sh
.venv/bin/python -m pytest -m live tests/live -q --tb=short -p no:cacheprovider
```

The suite runs QuotaMesh in-process against the real providers and maps each test to a user story
in [`user-stories.md`](user-stories.md). Settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `QM_ENV_FILE` | repository `.env` | Where to read keys from |
| `QM_LIVE_MAX_OPENAI` / `QM_LIVE_MAX_GEMINI` | 40 / 30 | Hard cap on generation calls per run |
| `QM_LIVE_OPENAI_MODEL` / `QM_LIVE_GEMINI_MODEL` | discovered | Override the inexpensive model choice |

Every chat request must carry a small token limit, Gemini calls are spaced at least 5 s apart for
free-tier rate limits, and a test is skipped (not failed) when a key is missing or a provider is
out of quota or overloaded after the classification of that error has been asserted.

## 3. Examples against a running gateway

Start an isolated gateway, import the keys, then run an example:

```powershell
# terminal 1
$env:QUOTAMESH_DATA_DIR = "$env:TEMP\qm-live"
.venv\Scripts\python.exe -m quotamesh start --port 8797 --no-browser
# terminal 2 (the same QUOTAMESH_DATA_DIR, so the CLI uses the same local key)
$env:QUOTAMESH_DATA_DIR = "$env:TEMP\qm-live"
.venv\Scripts\python.exe -m quotamesh import-env --file .env --port 8797
$env:QUOTAMESH_BASE_URL = "http://127.0.0.1:8797/v1"
$env:QUOTAMESH_API_KEY = (.venv\Scripts\python.exe -m quotamesh key --data-dir $env:QUOTAMESH_DATA_DIR)
.venv\Scripts\python.exe examples\chat_cli.py --once "Say hello in five words."
```

Delete the temporary data directory afterwards: it holds the imported keys in plaintext.

## Expected cost

A full probe plus a full live-suite run makes roughly 30 OpenAI and 20 Gemini generation calls with
replies of a few tokens. At `gpt-4.1-nano` pricing that is well under one US cent; Gemini free-tier
calls cost nothing. The hard caps above stop a run before it can exceed its budget.

## Reading results

- **Gemini 503 "high demand" or slow replies (15–30 s)** are provider-side; QuotaMesh classifies them
  as a short provider failure and falls back when a backup target exists.
- **Gemini `429 RESOURCE_EXHAUSTED`** with a per-day quota id blocks that model until Pacific midnight;
  per-minute limits cool down for the provider's `retryDelay`.
- **OpenAI `insufficient_quota`** marks the key unusable until you add credit and reset it.
- **A new error shape** means the provider changed: rerun the probe, add the sanitized fixture and a
  classifier expectation in `tests/test_classifier_providers.py`.
