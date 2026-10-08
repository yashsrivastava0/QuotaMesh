# Frontend experience and acceptance

This implementation follows the MVP PDF and ROADMAP. It serves developers who
already have API access. Guidance is built into the app and makes no AI calls.
There are no new API protocols, schema migrations, or runtime dependencies.

## Pages, features, and user stories

The saved profile is carried in `?profile=<slug>` across navigation. An editor
draft does not affect routing until saved. Save, Discard, and Stay protect drafts
on in-app navigation; browser reload/back uses the native unsaved-changes warning.

| Page | User story and acceptance |
| --- | --- |
| Overview `/` | As a developer, I can see grouped free/trial/paid access and honest usage, then follow a concrete next step. Shared trial keys appear as one source. Unknown quota stays unknown. |
| API access `/access` | I can add, edit, rotate, or remove access and preview environment references. Saving untested sends no provider request. Explicit listing never generates. First-run setup saves an exact model with paid access off. |
| Profiles `/profiles` | I can name an application, order exact model targets, use a pool or pin a key, and set spending rules. New profiles start with paid disabled and no inherited cap. Draft edits are labeled and cannot silently change the saved route. |
| Route & test `/route` | I can inspect which target is next and why others are skipped, follow a recovery link, then explicitly send a request. Streaming distinguishes fallback before commitment from interruption after output starts. |
| Connect `/connect` | I can see my local base URL and saved alias, set the gateway environment first, and copy the client snippet second. Profile selection changes the generated alias. |
| Activity `/activity` | I can filter and page request traces and expand ordered attempts/skips. History contains metadata, never message or completion bodies. |
| Help `/help` | I can understand add access → save rules → inspect → send → observe, including costs, cooldowns, truth labels, and limitations. |
| Doctor `/help/doctor` | I can explicitly list models or diagnose one selected target. Generation requires consent and obeys policy; discovered IDs are draft hints. |
| Catalog `/help/catalog` | I can consult dated provider information without interpreting a preset as proof of current quota or model access. |

## Journeys

1. **First connection:** API access → save untested or explicitly list → safe
   default setup → Route & test → Connect → Activity.
2. **New application:** Profiles → New → enter name/slug and exact targets →
   Save → inspect route → copy Connect instructions for `qm/<slug>`.
3. **Recover blocked access:** Route & test → reason and recovery link → edit
   access/profile or run Doctor → save → refresh dry run → explicitly test.
4. **Inspect a fallback:** send a test → see attempt count and selected target →
   Activity → expand trace → inspect cooldown and honest observed cost.

## Data loading and backend contract

The shell renders on the server and loads shared helpers plus only the current
page module. Asset URLs have a content digest to avoid stale scripts after updates.
Each surface has independent loading, stale, and retry feedback. Obsolete reads
are aborted; responses cannot overwrite a newer refresh. Local event updates are
debounced and do not replace dirty forms or a focused editor. No navigation or
dry run probes providers.

| Page | Initial management GETs |
| --- | --- |
| Overview | wallet, explain: 2 |
| API access / Profiles | status: 1 |
| Route & test | explain, status: 2 |
| Connect | integrations: 1 |
| Activity | activity, status: 2 |
| Help | 0 |
| Doctor | status, doctor: 2 |
| Catalog | catalog: 1 |

The previous shared refresh used seven management reads. The new counts exclude
HTML/static requests and the local event stream. This is a request reduction,
not a claim of a production latency benchmark.

Validation errors add sanitized `fields` with paths and fixed guidance, without
echoing submitted values. Explain adds `recovery_action` metadata from the same
selection result used by the gateway. Existing response fields remain available.
Loopback, session/bearer authorization, paid defaults, and stream commitment rules
are preserved.

## Synthetic test data

Run `quotamesh demo --no-browser` and use its printed one-time URL. This creates
a temporary store and a loopback fake upstream. The Overview and Route pages
provide a scenario picker with concrete instructions. Reset affects only the demo.

| Scenario ID | Expected behavior |
| --- | --- |
| empty | No credentials or profiles; follow first-run steps. |
| unconfigured | Access exists; create a safe default profile. |
| success | First free target succeeds, one attempt. |
| wallet | Five keys; two trial keys share a $20 source; three named profiles. |
| fallback | Primary rate limit then free backup, two attempts; cooldown persists. |
| expired | Expired trial skipped; backup succeeds in one attempt. |
| invalid | Rejected key then backup, two attempts. |
| missing-env | Absent process variable skipped; backup in one attempt. |
| paid-guard | Free failures; paid never contacted. |
| paid-cap | Explicit paid fallback reports $0.60; later calls blocked by $1 cap. |
| unknown-cost | Missing paid cost stays unknown; subsequent capped use blocks. |
| accounting | Incomplete accounting blocks paid access. |
| shared-quota | Shared cooldown skips sibling, one attempt. |
| error-first | First SSE error falls back before valid output. |
| midstream | Committed stream interrupts; no provider switch. |
| large | 38 sources with long Unicode labels for layout testing. |

## Repeatable checks

Install development and browser extras, then install Playwright Chromium:

```sh
python -m pip install -e '.[dev,browser]'
python -m playwright install chromium
python -m pytest -p no:cacheprovider -q
ruff check .
python scripts/check_javascript.py
python scripts/browser_smoke.py --screenshots output/frontend-browser
python -m build
python scripts/release_smoke.py --wheel dist/quotamesh-0.1.0-py3-none-any.whl
```

Use `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on Unix.
An installed Chrome/Chromium can replace the download:

```powershell
.venv/Scripts/python.exe scripts/browser_smoke.py --browser-executable 'C:/Program Files/Google/Chrome/Application/chrome.exe' --screenshots output/frontend-browser
```

The browser script owns its temporary servers/data and exits nonzero on failure.
It writes a JSON report and screenshots; failures also produce a screenshot and
trace. It checks all nine pages at 1440, 768, and 390 pixels, narrower CSS viewport
reflow equivalent to a 200% zoom layout, keyboard focus, first-run setup, CRUD,
draft navigation, routing, diagnostics, large data, API outages, malformed responses,
session expiry, and empty browser storage. The reflow check is not actual browser
zoom or a full accessibility audit. CI uploads the report even on failure.

SDK checks remain in `scripts/integration_smoke.py`; install the verification SDKs
as described in README. `scripts/record_demo.py` records a synthetic walkthrough.
Screenshots, recordings, temporary keys, and package output are ignored by Git.

## Local evidence (2026-10-07)

- 142 pytest tests passed on Windows/Python 3.14.
- Ruff and syntax checks for every bundled JavaScript file passed.
- Browser suite passed with system Chrome, nine pages, twelve request scenarios,
  and zero uncaught JavaScript errors; 27 responsive page screenshots produced.
- Python/Node OpenAI clients, Python streaming, generated Bash/cURL and PowerShell
  requests, and OpenCode-compatible transport passed against the fake upstream.
  This SDK run used the development installation, not a clean wheel installation.
- Isolated sdist/wheel build passed. Clean wheel/uvx process acceptance passed:
  streaming, two keys on one provider, restart, single-owner lock, and privacy.
- Synthetic walkthrough recording completed. Interactive Edge review confirmed
  the light overview and navigation to Route & test.

These results cover synthetic traffic. Live provider accounts were not exercised.

### CI follow-up (2026-10-08)

The merged frontend's CI passed the eight Windows/macOS/Linux Python jobs and
installed-wheel client checks. Browser acceptance failed because the copy check
read its status before the asynchronous clipboard operation completed. The
follow-up waits for the completion state, verifies actual clipboard contents with
explicit test permission, and separately simulates delayed permission rejection
to verify selection and focus for manual copying. This changes the test harness;
the application's existing copy fallback is preserved.
