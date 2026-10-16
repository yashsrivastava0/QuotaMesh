# Live-readiness API contracts

Frozen interfaces shared by the engine, platform, frontend and live-test work on
`feat/live-provider-readiness`. Management endpoints accept the dashboard session cookie or the
local bearer key unless stated otherwise, return OpenAI-shaped errors
(`{"error": {"message", "type", "code"}}`), and never return provider secrets.

## Session

`POST /api/session/link` — **bearer key only** (cookie rejected).
Response `201 {"url": "http://127.0.0.1:<port>/bootstrap?token=<token>", "expires_in": 120}`.
The token is single-use and expires after 120 s. CLI: `quotamesh open [--port] [--print]`.

`POST /api/local-key/reveal` — **dashboard session only**, same-origin.
Response `200 {"key": "<local gateway key>"}` with `Cache-Control: no-store`.

Session cookie name: `qm_session_<port>`. `/bootstrap` with an already valid session redirects
(303) to `/` without consuming the token; an invalid or used token returns a small HTML page
telling the user to run `quotamesh open`.

## Setup and spend

`POST /api/setup` request (all new fields optional):
```json
{"credential_id": 1, "model": "gemini-3.5-flash-lite",
 "allow_paid": false, "paid_daily_cap_usd": null, "paid_monthly_cap_usd": null,
 "input_price": null, "output_price": null, "allow_unknown_price": false}
```
Response `201 {"alias": "qm/default", "allow_paid": false}`; `409 setup_conflict` when the default
profile already has targets. Prices are USD per million tokens.

`POST /api/profiles/{slug}/reconcile` `{"amount_usd": 0.12, "scope": "today" | "month"}` →
`200 {"cleared_days": 1, "amount_usd": 0.12}`. Clears `unknown_cost` for that scope and records the
amount as a MANUAL observation.

`POST /api/accounting/acknowledge` `{"confirm": true}` → `200 {"accounting_failed": false}` after a
successful test write; `503` if storage is still failing.

New profiles default to `nonstream_deadline_s = 90` and `first_event_timeout_s = 45`
(see `docs/live-provider-notes.md`).

## Environment import

`POST /api/environment/parse` `{"content": "<.env text, ≤ 64 KiB>"}` →
```json
{"entries": [{"name": "GEMINI_API_KEY", "line": 3, "provider_id": "gemini",
              "status": "new", "fingerprint": "d9ba6152", "length": 53,
              "suggested_plan": "FREE"}],
 "warnings": [{"line": 7, "message": "Expected NAME=value; line ignored"}]}
```
`status` ∈ `new | configured | duplicate | placeholder | unsupported | invalid`
(`configured`: the same secret is already saved; `duplicate`: the same value appears under an
earlier name in this file). Values are never returned. `400` for unreadable content, `413` above
64 KiB.

`POST /api/environment/import-file`
```json
{"content": "<same text>", "store": "value" | "reference",
 "items": [{"name": "GEMINI_API_KEY", "plan_type": "FREE", "label": "Gemini personal",
            "quota_group": "gemini-personal"}]}
```
→ `201 {"imported": [{"name", "credential_id", "provider_id", "plan_type"}],
"skipped": [{"name", "reason"}], "warnings": [{"name", "message"}]}`. The server re-parses the
content and never stores it; it never accepts a file path. CLI:
`quotamesh import-env --file .env [--plan NAME=PLAN ...] [--store value|reference] [--yes]`.

## Observability

`GET /api/requests/{request_id}` (32 lowercase hex) →
```json
{"request_id": "…", "profile_slug": "default", "started_at": "…",
 "attempts": [{"attempt_idx": 1, "ts": "…", "provider_id": "gemini", "model": "…",
               "credential_label": "…", "plan_type": "FREE", "outcome": "OK",
               "http_status": 200, "error_class": null, "latency_ms": 812, "ttfb_ms": 640,
               "input_tokens": 8, "output_tokens": 1, "provider_cost_usd": null,
               "estimated_cost_usd": 0, "streamed": 1,
               "skipped": [{"label": "…", "skip_reason": "cooldown", "explanation": "…"}]}],
 "totals": {"attempts": 1, "input_tokens": 8, "output_tokens": 1,
            "known_cost_usd": 0, "cost_source": "LOCAL" | "PROVIDER" | "UNKNOWN"}}
```
`404` when unknown or pruned. Metadata only — never prompt or reply text.

`GET /api/wallet` gains `daily` — 14 entries, oldest first, zero-filled:
```json
{"day": "2026-10-16", "routed_requests": 3, "attempts": 4, "failures": 1,
 "input_tokens": 120, "output_tokens": 40, "known_cost_usd": 0.0002, "unknown_cost": 0,
 "by_provider": {"gemini": {"routed_requests": 2, "known_cost_usd": 0},
                 "openai": {"routed_requests": 1, "known_cost_usd": 0.0002}}}
```

Every `recovery_action` object (Explain, Activity, blocked responses) gains a relative `href`
(for example `/access?profile=default&credential=3`) pointing at the page that fixes it.

## Gateway

- `/v1/models` items: `{"id": "qm/<slug>", "object": "model", "created": <epoch int>,
  "owned_by": "quotamesh"}`; `GET /v1/models/{id}` returns one item or a `404` OpenAI error.
- Blocked (`429`/`503`) responses carry `X-QuotaMesh-Profile` and `X-QuotaMesh-Request-Id`; each
  `quotamesh.candidates[]` item adds `explanation` and `recovery_action`.
- Deadline `504`: `{"error": {"message", "type": "deadline_exceeded", "code": "deadline_exceeded"},
  "quotamesh": {"attempts": n, "last_error_class": "…" | null, "last_http_status": n | null}}`,
  plus `X-QuotaMesh-*` headers when an attempt was made.
- Streams to `stream_usage = "inject"` providers get `stream_options.include_usage` when the client
  did not ask; the response then carries `X-QuotaMesh-Usage-Injected: true`, and an OpenAI-style
  usage-only chunk (`"choices": []`) is not forwarded to that client.
- Error codes the dashboard explains: `unauthorized`, `invalid_model`, `profile_not_found`,
  `not_configured`, `paid_blocked`, `no_candidates`, `deadline_exceeded`, `request_too_large`, and
  upstream codes passed through as received (for example `invalid_api_key`,
  `unsupported_parameter`, `model_not_found`).

## Doctor

`POST /api/doctor` results add, for generation checks, `finish_reason`, `output_tokens` and
`reply_chars` (metadata only), and for listings `skipped_ids` (count of identifiers that failed
validation). Listing model ids are returned without the registry `model_list_prefix`.
