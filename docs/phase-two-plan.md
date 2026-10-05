# Phase two implementation and acceptance plan

Task: `phase-two`. Product source: the complete 65-page v0.3 PDF; the repository's
five-phase roadmap defines this deliverable. Phase two is the decision engine,
not the entire product MVP. Named profiles and a full wallet remain phase three.

## Baseline and research

- Phase one contains a working authenticated localhost Chat Completions proxy,
  bootstrap/session protection, HTTPS endpoint validation, blocked redirects,
  metadata-only SQLite history, and first-event streaming transport.
- Baseline: six tests passed; the seventh patched an obsolete module location.
  Correct the test to exercise the actual body-size boundary before extending it.
- PDF sections 7–12 and 17 define order, failure classification, scoped state,
  stream commitment, cap behavior, expiry, and the acceptance matrix.
- Google OpenAI-compatibility and rate-limit documentation fetched successfully
  on 6 October 2026 confirms the endpoint and project/model scope with Pacific
  midnight daily resets. OpenAI's rate-limit docs confirm duration reset headers.
- Groq and NVIDIA documentation requests returned HTTP 403 here. Their presets
  are compatibility conveniences, with synthetic classifier fixtures, not a
  claim of live verification. No quota numbers or volatile prices are bundled.

## Work sequence

1. **Foundation:** fix the phase-one test reference, preserve existing security
   and raw extension fields, and validate the baseline.
2. **Pure engine:** enumerate saved targets and sticky priority pools, return
   eligible and skipped candidates from the same function used by live/dry-run,
   classify status + body + headers, and compute quota transitions.
3. **Persistence:** additive schema migration; persist credential invalidity and
   quota-group/model cooldown/exhaustion; retain daily paid spend independently
   of request history; validate expiry and price overrides.
4. **Transport:** recompute before each attempt, count only upstream calls, avoid
   same key/model retries, enforce an overall pre-commit deadline, skip a target
   after context errors, and preserve upstream responses. Never switch providers
   after a role-only or content response event.
5. **Testing UI:** expose a small credential list, ordered default route editor,
   policy controls, readable decisions, and an explicit generation test. Add a
   separate temporary-data demo with no real secrets or billable requests.
6. **Acceptance:** test the PDF's failure/paid-safety matrix, persistence,
   concurrency, malformed streams, log failure, security, packaging, and a
   running loopback demo. Document actual results and remaining live-provider
   checks. Save installation/startup instructions for cloud task reuse.

## Deliberate boundaries and assumptions

- One `qm/default` policy; multiple targets and provider pools are required to
  exercise phase two. A full named-profile/wallet subsystem is not needed here.
- Targets remain in saved order; pool priority sorts ascending, then stable ID.
  A null pinned credential means the matching provider pool.
- A missing quota group conservatively shares one provider group. Groq defaults
  to separate credential groups, per the PDF; explicitly group shared projects.
  Independent labels do not manufacture independent quota.
- Explicit Retry-After takes precedence. Unknown 429s use the PDF ladder and
  remain COOLDOWN. Daily exhaustion requires both daily evidence and a reset.
- Credential/model 403 and provider/model transient blocks last 30 seconds in memory. Auth and billing
  failures persist until an explicit recovery action. Resetting a credential
  clears its shared quota group; the UI must say so.
- Paid/unknown-plan credentials require explicit paid permission. Optional daily
  and monthly caps use UTC calendar windows and traffic through QuotaMesh only.
  Caps check observed spend before selection, not provider billing totals or
  guaranteed preflight reservations. Concurrent/in-flight cost can overshoot.
- No speculative model prices. Users can supply both USD-per-million token
  prices. Explicit provider `usage.cost_usd` (USD) wins over the local token-price estimate.
  Missing token usage stays unknown. With a cap, unknown price/spend blocks paid
  routing unless the user explicitly opts out of that protection.
- Dollar rollups are the minimal phase-two accounting prerequisite, not full
  wallet statistics. Attempt pruning must never erase spend used by caps.
- SSE comments are not commitment; malformed or error-first streams can fail
  over. Missing `[DONE]` after commitment is reported as interruption. Streaming
  usage is captured only if returned; no unsupported request field is injected.
- No Responses/Messages translation, model ranking, background probes, scraping,
  automatic price updates, external services, or automatic `.env` loading.
- Provider-free tests establish engine behavior, not real-account connectivity
  or production readiness for every compatible endpoint.


## Validation evidence

- Phase-one baseline issue corrected; authenticated raw pass-through, Host/origin checks,
  redirect blocking, body size, stream commitment, and secret/body redaction remain covered.
- 69 automated tests currently pass. Coverage includes the PDF failure/paid-safety matrix,
  body-aware Gemini errors, documented reset headers, scoped model-access blocks, pool
  ordering, unknown cost handling, expiry, attempt budgets, both deadlines, concurrent
  routing after cooldown, late-success overlap, cancellation, and additive v1 migration.
- Real Chromium smoke verifies all six loopback demo scenarios, saving policy fields,
  adding credentials/targets, rendered streaming/error output, and mobile overflow.
- Ruff and JavaScript syntax checks pass. Source/wheel builds and a fresh installed-wheel CLI/dashboard/fallback smoke pass; live provider requests remain unrun without user keys.
