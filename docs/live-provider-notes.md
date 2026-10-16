# Live provider notes

Evidence from `scripts/live_probe.py` on 2026-10-16 (Windows 11, httpx 0.28.1). Calls went
directly to each provider with token-capped prompts; QuotaMesh was not involved. Sanitized
responses are in `tests/fixtures/providers/<provider>/` (ids, output text, thought signatures and
account identifiers redacted; no key material). Generation calls used: 11 (OpenAI 5, Gemini 6).

| Provider | Endpoint | Model used | Account |
| --- | --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-nano` (133 models listed) | project key (`sk-…`) |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-3.5-flash-lite` (63 models listed) | AI Studio key in the new `AQ.` format |

## Decisions

**D1 — Stream end.** Both providers end streams with `data: [DONE]` using LF framing. QuotaMesh
keeps `[DONE]` as the normal end marker and also accepts a clean EOF after a `finish_reason` was
seen, so providers that omit the marker do not get a synthetic error appended.

**D2 — Usage placement.** Neither provider sends usage in a stream unless the request has
`stream_options.include_usage`. With it:
- OpenAI sends `"usage": null` on every chunk and one final chunk with `"choices": []` and usage.
- Gemini attaches cumulative usage to every content chunk (no separate empty-choices chunk).

**D3 — Usage injection.** Both accept `stream_options.include_usage`, so both get
`stream_usage = "inject"`. When QuotaMesh injected the option, the OpenAI usage-only chunk is
dropped before it reaches a client that did not ask for it; Gemini's usage rides on content chunks
and is passed through unchanged (an extra `usage` field is valid on `chat.completion.chunk`).

**D4 — Token limit parameter.** GPT-5-family models reject `max_tokens`
(`400 unsupported_parameter`, "Use 'max_completion_tokens' instead"). Gemini accepts both
`max_tokens` and `max_completion_tokens`. Doctor and the Playground send `max_completion_tokens` for
OpenAI and Gemini (`token_param` in the registry); unverified presets keep `max_tokens`.

**D5 — Gemini model ids.** The listing returns `models/<id>`; chat accepts both `models/<id>` and
the bare `<id>`. Pickers strip the prefix (`model_list_prefix = "models/"`).

**D6 — Error shapes.**
- OpenAI: an object `{"error": {"message", "type", "code", "param"}}`; invalid key → `401
  invalid_api_key`; unknown model → `404 model_not_found`.
- Gemini: a **root array** `[{"error": {"code", "message", "status"}}]` for 400/404/503. An invalid
  key is `400 INVALID_ARGUMENT` "Please pass a valid API key"; unknown model `404 NOT_FOUND`;
  overload `503 UNAVAILABLE` "This model is currently experiencing high demand…".
- The pre-existing classifier read only object bodies, so a bad Gemini key was a terminal
  `request_error` (no fallback, key never marked invalid).

**D7 — Thinking tokens.** For `gemini-3.5-flash-lite`, `total_tokens = prompt_tokens +
completion_tokens`; no hidden thinking tokens were observed. Usage accounting still bills
`max(completion, total − prompt)` as output when a provider reports a larger total.

**D8 — Gemini authentication.** The `AQ.` key works with `Authorization: Bearer` on the
OpenAI-compatible endpoint (200) and with `x-goog-api-key` on the native endpoint (200). The
OpenAI-compatible endpoint answers `404 "Requested entity was not found."` when the key is sent as
`x-goog-api-key`. QuotaMesh keeps Bearer; no auth-header quirk is needed. Some accounts report
`401 ACCESS_TOKEN_TYPE_UNSUPPORTED` for `AQ.` keys; that is classified as an invalid credential with
an explanation.

## Other observations

- **Latency.** Gemini non-stream calls took 15–28 s for a one-word reply, streams 3–7 s, and one
  call returned `503 UNAVAILABLE` (high demand). OpenAI calls took 0.8–1.3 s. New profiles default to
  a 90 s non-stream deadline and a 45 s first-event timeout; streaming is recommended for Gemini.
- **Gemini tool calls.** With `tool_choice: "required"`, the stream carried a complete tool call in
  one delta **without an `index` field**, and the final chunk used `finish_reason: "stop"` (not
  `"tool_calls"`). Gemini 3 also returns `extra_content.google.thought_signature`, which clients must
  send back unchanged with the assistant tool-call message on the next turn. QuotaMesh passes these
  shapes through unchanged; the examples handle them.
- **OpenAI streams** include an `obfuscation` padding field and report the dated model id
  (`gpt-4.1-nano-2025-04-14`).
- **OpenAI rate-limit headers** use durations such as `12ms` and `0s`
  (`x-ratelimit-reset-requests`), which the passive observation parser already handles.
