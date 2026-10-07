# QuotaMesh 0.1.0 - final testing guide

Release target: Windows, macOS, and Linux; Python 3.11 or newer. Phase 5 is the
final MVP phase. This guide describes expected behavior; actual run results and
publication status belong in `docs/release-evidence.md`.

## 1. What is included

QuotaMesh connects API access you own to local projects through one authenticated
Chat Completions endpoint. It includes a Capacity Wallet, named ordered profiles,
key pools, shared cooldowns, expiry, paid permissions/caps, dry-run explanations,
integration snippets, explicit Doctor checks, environment-reference import, dated
provider links, metadata history, an isolated demo, and release packaging.

The service runs on 127.0.0.1 with one worker. Configuration and observations stay
local. There is no telemetry, background quota probing, or frontend build step.
Requests and completions are transient and never stored in history. Directly saved
keys are plaintext in local SQLite; environment references avoid storing key values.

Native Responses/Messages, encrypted/keyring storage, arbitrary .env import,
embeddings/audio/images/video, model ranking, smart routing, teams, and hosted mode
are outside this finalized MVP. These exclusions follow the accepted PDF scope;
they are not unfinished Phase 5 tasks or promises of a Phase 6.

## 2. Install and identify the version

After PyPI publication, install uv and run:

```sh
uvx quotamesh --version
uvx quotamesh start
```

Expected version: `0.1.0`. The CLI prints a one-time local dashboard URL and opens
the browser after startup. Login consumes that URL and redirects to `/` with an
HttpOnly, SameSite Strict session cookie. Reusing the bootstrap link returns 403.
The default gateway is `http://127.0.0.1:8787/v1`.

Before publication, use the release wheel without an editable checkout:

```sh
uvx --from ./dist/quotamesh-0.1.0-py3-none-any.whl quotamesh --version
uvx --from ./dist/quotamesh-0.1.0-py3-none-any.whl quotamesh start
```

For a persistent installation, use `uv tool install quotamesh` or
`python -m pip install quotamesh`. Use `quotamesh` directly afterward. For command
examples below, replace `quotamesh` with the full uvx invocation when using only
ephemeral installation. `--no-browser` supports terminal-only startup.

## 3. Test safely without provider credentials

```sh
quotamesh demo
```

Expected: an isolated dashboard on port 8788, temporary data, fake credentials,
simulated dollars, and a separate local gateway key. The printed
`quotamesh key --data-dir <temporary directory>` command accesses that demo's key.
The normal user's data is not changed. Stop the process with Ctrl+C.

Open My capacity and inspect FREE, TRIAL / CREDITS, and PAID / UNKNOWN. Two trial
keys share one $20 starting-credit source. Expand Usage, cost, and timing evidence
to see token observations and honest unknowns. No numeric remaining provider quota
is fabricated. Trial expiry is manual; local dollar estimates include only gateway
traffic. The selected profile controls the observed paid-spend summary.

## 4. First real connection

1. Start `quotamesh start` and open its one-time URL.
2. Choose the provider and explicitly choose your actual plan. Enter either a key
   or a process-environment variable name. Custom endpoints require an HTTPS base
   URL; loopback HTTP is allowed for testing. Label is optional.
3. Use Save as untested to avoid a provider call, or Save and test listing access
   to explicitly request models. Listing never silently triggers generation.
4. In Finish your default connection, choose the saved credential and enter an
   exact model ID. Discovery supplies hints, not a guarantee of generation access.
5. Create the safe default profile. Paid permission remains off, including when
   the credential is classified PAID or UNKNOWN. This action makes no generation
   call. An existing default target cannot be silently replaced by setup.
6. Review Available now. A paid key will be blocked until the profile is edited
   deliberately. A free or permitted trial key can be eligible without proving
   remaining quota. Expired access is excluded.
7. Select Connect, choose your shell/client, copy environment setup and then the
   client snippet. Use `qm/default` or a named saved alias, never a project URL path.
8. Send an explicit test or SDK request. Check its returned route headers and
   expandable history. Response text is shown transiently, not persisted.

Do not paste provider credentials into chat or commit them. Set environment
variables before starting the server; the server cannot observe changes made in a
different process after startup. Changing a reference's value requires restart.

## 5. Manual acceptance scenarios

### Routing and spending

| Action | Expected result |
| --- | --- |
| Load Rate limit -> free backup and send a request | HTTP 200, two attempts, first source cooling, backup selected. |
| Send another request while the primary cools | Backup serves directly; no sleeping through cooldown. |
| Load Shared project -> sibling skipped | Same group/model block excludes the sibling; only one upstream attempt. |
| Load Both free sources fail -> paid blocked | Paid source is skipped and never contacted. |
| Load Paid backup -> $1 daily cap | Simulated $0.60 calls reach/overshoot the cap; later paid calls are blocked. |
| Remove manual token prices from a capped paid target | Unknown-price guard blocks it unless explicitly overridden. |
| Disallow trial or enter a past expiry | Trial or expired access is excluded with its reason. |
| Reorder targets, inspect, and save | Saved order controls live traffic and Explain; there is no hidden ranking. |
| Add an identical key for the same provider/endpoint | Sanitized conflict; edit the existing credential. |
| Disable/delete a pinned key | Disable is allowed; deletion requires removing the pin. History remains. |
| Rotate a key | Existing spend and shared cooldowns survive; stale diagnostics are cleared. |

Caps are local, scoped to profiles, and use UTC day/month boundaries. They check
observed cost before selection. In-flight calls may overshoot; caps are not provider
billing controls. Provider-reported USD wins over local token-price estimates.
Missing cost or usage stays unknown. Abstract credits are not converted to dollars.

### Streaming, diagnostics, and frontend

| Action | Expected result |
| --- | --- |
| Load First SSE error -> backup, enable stream, send | Fallback happens before the first valid response event. |
| Load Mid-stream cut, enable stream, send | Stream reports interruption; no provider switch after commitment. |
| Stop an active request or Doctor generation | Upstream work closes; no hidden backup generation is started. |
| Refresh Explain or navigate between sections | No provider probe; saved policy and eligibility remain consistent. |
| Run Doctor listing | Bounded explicit listing check; success does not prove quota or generation. |
| Run Doctor generation without consent | Rejected locally; no upstream request. |
| Run consented Doctor generation on a paid-blocked target | Blocked by the same profile policy and caps. |
| Preview/import environment | Names and presence only; known variables become references with UNKNOWN plan. |
| Edit targets, expand history, then refresh | Draft targets and expanded evidence/traces remain intact. |
| Switch profiles quickly | Old responses do not overwrite the currently selected profile. |
| Copy a snippet with clipboard access denied | Text is selected for manual copying; no silent failure. |
| Use keyboard only and reduce-motion preference | Visible focus, skip link, labeled controls, usable navigation. |
| Resize to phone, tablet, desktop | Forms fit; only intentionally scrollable tables/code overflow internally. |

## 6. Truth labels and passive evidence

- PROVIDER: directly returned usage or documented response-header evidence.
- LOCAL: gateway counts, measured timing, or token-price dollar estimates.
- MANUAL: plan, starting credit, expiry, grouping, and user-entered prices.
- UNKNOWN: no reliable evidence. Unknown is not zero or free.

Passive rate observations are captured only from real routed traffic for providers
whose header semantics are documented. Groq request headers describe a day window;
token headers describe a minute window. OpenAI values retain a generic provider-
window label. These are snapshots, not live balances, and become stale at the
earlier of the reported reset or 60 seconds. Other traffic may consume them sooner.

First-response timing measures response headers for non-streaming requests and
the first valid response event for streaming. Older attempts have unknown timing.
Model-list results become visibly stale after 24 hours. All observations include
source and timestamp; refreshing the UI does not refresh provider evidence.

## 7. Security, lifecycle, and recovery

Wrong bearer keys, foreign Host headers, and foreign-origin management writes are
rejected. Remote HTTP endpoints and credentials/queries embedded in URLs are
rejected. Redirects are not followed with provider secrets attached.

Start another gateway using the same data directory, even on another port:
startup must fail with an ownership message. Stop the first gateway and restart:
the OS-held lock is released, while configuration, cooldowns, history, and usage
survive. An occupied port should produce a clear binding error.

Data defaults to `%LOCALAPPDATA%/QuotaMesh` on Windows or
`~/.local/share/quotamesh` on Unix-like systems. `QUOTAMESH_DATA_DIR` overrides it.
Unix permissions are restricted; Windows POSIX mode bits are not equivalent ACLs.

Before upgrading, stop QuotaMesh and copy the entire data directory to a private
backup. Schema versions 1-4 migrate to version 5. Paid totals are preserved; legacy
history coverage stays honestly labeled. Older builds cannot read schema v5.
For rollback, stop the new build and restore the pre-upgrade backup with the old
build. Never downgrade just the schema version number.

History retains 30 days or 50,000 attempts, with complete request traces at the row
boundary. Daily usage survives pruning and profile/key archival. Rejected calls
with no upstream attempt are not counted as routed requests.

If paid accounting fails, completed responses still reach callers, diagnostics
increase, and paid traffic fails closed. Repair storage and reconcile missing spend
before removing the `paid-accounting-incomplete` safety marker. Resetting a key
does not clear spend or that marker and does not restore provider quota.

## 8. Repeatable automated checks

From a development checkout with the dev dependencies installed:

```sh
python -m pytest -p no:cacheprovider -q
ruff check .
node --check src/quotamesh/ui/static/dashboard.js
uv build
python scripts/release_smoke.py --wheel dist/quotamesh-0.1.0-py3-none-any.whl
python scripts/browser_smoke.py
python scripts/integration_smoke.py
python scripts/record_demo.py
```

On Windows, the repository environment's Python is `.venv\Scripts\python.exe`;
on Unix it is `.venv/bin/python`. Browser checks require Playwright and Chromium.
Set `CHROMIUM_PATH` to an installed Chromium if needed. SDK verification requires
Python `openai` plus Node `openai` and `@ai-sdk/openai-compatible`; these are test
tools, not gateway runtime dependencies. See README for their installation.

The release smoke check launches real installed/uvx processes with temporary data,
checks two keys on one provider, fallback, streaming, restart, privacy, and the
single-owner lock. CI exercises Windows/macOS/Linux with Python 3.11 and 3.14 and
also tests Python 3.12/3.13 on Linux. Remote platform results are reported only
after the checks actually pass.

## 9. Complete PDF checklist mapping

| PDF requirement group | Implementation / repeatable evidence |
| --- | --- |
| uvx, one process, local infrastructure-free launch | CLI, OS-held lock, release_smoke, platform CI |
| Random bearer, one-time dashboard session, Host/Origin defenses | security.py, gateway tests, browser bootstrap |
| Wallet FREE/TRIAL/PAID and source truth | wallet.py, phase-three tests, mixed-wallet browser demo |
| Minimal add/test, shared groups, expiry | credential forms, Doctor, phase-three/four/five tests |
| One profile concept and qm aliases | profile CRUD, Chat Completions gateway, installed SDK tests |
| Streaming and non-streaming pass-through | gateway tests, browser failures, SDK/release smoke |
| Key pools, quota/model cooldowns, body-aware classifiers | engine tests and phase-two integration matrix |
| Paid opt-in, caps, unknown costs | selector, durable accounting, phase-two/three/four tests |
| Available Now/Explain/live parity | shared runtime snapshot and phase-four tests |
| Python/Node/cURL/env/OpenCode integration | Connect templates, installed client acceptance |
| Metadata-only history, every routed fallback/skip | activity traces, privacy tests, expandable browser history |
| Manual Doctor and dated data-only catalog | phase-four/five checks and nine-entry catalog |
| Honest unknown 429 and exact stream commitment | classifiers, quota persistence, pre/mid-stream tests |
| Durable rollups despite pruning | migration, pruning, archival, and usage tests |
| No full keys in logs/API; no redirects | privacy scans, synthetic process logs, gateway tests |
| Safe demo and accurate public positioning | demo scenarios/recording, README and this guide |

The PDF's optional direct model aliases are omitted to keep every request inside a
saved profile. `/v1/models` lists callable profile aliases; discovered upstream IDs
are editor hints. Manual prices are used instead of a bundled volatile price list.
The normal frontend uses vanilla JavaScript instead of HTMX. These are documented
implementation assumptions, not additional pending phases.

## 10. Provider and client compatibility

Gemini, Groq, OpenAI, and NVIDIA NIM have generic transport and tested classifier
fixtures. Anthropic compatibility, xAI, OpenRouter, Cerebras, and Mistral are dated
compatibility presets. Custom endpoints use generic behavior. Preset metadata does
not claim a live account, current model access, free quota, or every API feature.

Python/Node Chat Completions and generated shell clients are acceptance targets.
OpenCode uses its OpenAI-compatible transport/configuration; evidence distinguishes
transport checks from testing the full application. Native Codex and Claude Code
protocols are not claimed. Provider-compatible JSON extensions remain intact.

Official sources reviewed for the release include:

- https://docs.astral.sh/uv/guides/tools/
- https://docs.pypi.org/trusted-publishers/using-a-publisher/
- https://console.groq.com/docs/rate-limits
- https://developers.openai.com/api/docs/guides/rate-limits
- https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk
- https://docs.x.ai/developers/rest-api-reference/inference/chat-completions
- https://openrouter.ai/docs/api_reference/overview
- https://inference-docs.cerebras.ai/api-reference/chat-completions
- https://docs.mistral.ai/api/endpoint/chat

Publication requires the repository's `release.yml` Trusted Publisher to be
configured in the authorized PyPI account. Verify publication and clean index
installation before claiming that bare `uvx quotamesh start` is available publicly.
