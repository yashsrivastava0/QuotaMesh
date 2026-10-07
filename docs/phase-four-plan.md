# Phase four: Explain and connect

Task: phase-four. All 65 pages of the v0.3 PDF were reviewed before implementation.
The repository roadmap places Access Catalog in Phase 4 and reserves publishing,
clean-machine/cross-platform release acceptance and post-MVP protocols for Phase 5.

## Delivered behavior

- Available Now and Explain use the live selector, one state/clock snapshot, saved
  target/pool order, sanitized skip reasons, cap evidence and known recovery times.
  Unallocated credentials are informational and never silently join another provider's
  target. Fresh installations return an explicit unconfigured default state.
- Connect generates Python/Node SDK, Bash/cURL, PowerShell HTTP, environment and OpenCode
  instructions for the selected saved alias and actual port. No key enters its response.
  Temporary demos use `quotamesh key --data-dir` to read their isolated local key.
- Doctor runs only on explicit action. Listing has a 10-second deadline, 1-MiB response
  bound and 1,000-ID maximum. Redirects, malformed IDs and credential echoes are rejected.
  Unsupported listing never triggers a generation call. Listing 429s do not fabricate
  generation-model quota state; successful listing does not clear shared cooldowns.
- Explicit generation needs consent and a saved target/key. The existing gateway
  pipeline is constrained to one candidate/attempt; paid/trial permissions, expiry,
  caps, unknown-cost blocking, state changes and durable accounting still apply.
  Disconnect/cancellation closes work; raw completion/error bodies are not diagnostic data.
- Schema v4 transactionally adds latest checks per credential/mode. Only outcome,
  HTTP status, time, latency, discovered IDs and optional request ID are retained.
  Revision/endpoint guards reject late results after rotation/deletion; changing keys,
  provider or endpoint invalidates discovery. Observations become stale after 24 hours.
- Environment import inspects only five known names in the running server environment.
  It returns presence/names, stores references, skips duplicates and defaults to UNKNOWN.
  It does not load `.env` files, make provider calls, or change targets/permissions.
- CLI adds Doctor, profile dry run, client environment, add, import-env, reset and tail.
  Online commands use the running local management API and support explicit ports.
  Browser startup waits for server readiness; key output remains machine-readable.
- Local activity uses bounded SSE subscribers/queues and refresh hints after overflow
  or reconnect. No activity subscription or dashboard refresh probes a provider.
- The existing frontend now includes all workflows, copy fallback, complete async
  states, responsive forms, keyboard focus/skip navigation and reduced-motion handling.
  Stale profile responses are ignored; ordinary refreshes preserve draft targets and
  deletion confirmation. Missing draft pins remain explicit rather than becoming pools.

## Assumptions and sources

Eligibility is a policy decision, not a guarantee of available quota, Chat Completions
support, or successful generation. Known upstream model IDs are profile-editor hints;
`/v1/models` advertises callable profile aliases to preserve the guarded gateway contract.
No direct arbitrary-model routing, Responses/Messages translation, model ranking, live
billing integration, pricing crawler, keyring or additional product dependencies were added.

The six-entry Access Catalog is links/notes data, independent of routing. Existing preset
providers are distinguished from Cerebras/OpenRouter catalog-only entries using Custom
setup. Dates refer to official documentation/access-link review, not account verification.

Official references reviewed on 2026-10-07:

- [OpenCode custom providers](https://opencode.ai/docs/providers/) and
  [environment substitution](https://opencode.ai/docs/config/): use
  `@ai-sdk/openai-compatible` and `{env:QUOTAMESH_KEY}` for Chat Completions.
- [OpenAI Python SDK](https://developers.openai.com/api/reference/python) and
  [JavaScript Chat Completions](https://developers.openai.com/api/reference/typescript/resources/chat/subresources/completions).
- [Gemini OpenAI compatibility and listing](https://ai.google.dev/gemini-api/docs/openai),
  [Groq OpenAI compatibility](https://console.groq.com/docs/openai), and
  [NVIDIA model reference](https://docs.api.nvidia.com/nim/reference/models-1).
- [Cerebras documentation](https://inference-docs.cerebras.ai/) and
  [OpenRouter documentation](https://openrouter.ai/docs/quickstart), with their linked
  access pages. No volatile free-limit numbers are bundled.

## Verification

- Final local results: **125 tests passed**, Ruff and JavaScript syntax checks passed,
  Chromium acceptance reported zero JavaScript errors, and both source distribution
  and wheel built successfully. Installed-wheel client checks also passed.
- Baseline: 95 earlier-phase tests and original Chromium acceptance passed.
- Expanded automated checks cover passive explanation/live parity, initial setup,
  diagnostic classification, redirects, bounds, timeout/missing-secret/expiry,
  consent, single-target generation, paid caps, storage failures, cancellation,
  rotation races, stale discovery, reference import, authorization, queue bounds,
  v1/v2/v3 migrations, rollback and privacy.
- Browser acceptance covers a fresh empty store through Test/Save, discovered model
  choice, default profile save and first proxied response. It also exercises mixed
  capacity, profile/credential CRUD, streaming/history, all six original failures,
  Doctor, cap blocking, snippets/copy fallback, rapid profile switches, draft retention,
  keyboard access and 390/768/1440-pixel overflow checks.
- The installed wheel is tested from an isolated environment. Generated Python and
  Node SDK requests, Python streaming, Bash environment/cURL, PowerShell environment/
  HTTP, OpenCode-compatible transport, CLI commands and packaged assets are exercised
  against a fake loopback provider. The full OpenCode application is not claimed tested.
- `scripts/record_demo.py` produced a **20.48-second** WebM showing capacity, scripted
  429 fallback, paid blocking, explicit capped paid permission and connection code.
  `output/` artifacts and temporary environments are ignored by Git.

No authorized real-provider keys, provider quota or normal user data were used. Local
checks run on Windows/Python 3.14; Python 3.11/3.12 and browser CI remain configured,
with an added installed-wheel/client job. Remote CI is not claimed run by this task.
