# User stories and their evidence

Who QuotaMesh is for, what each person needs from it, and which automated test or manual
path proves each story. Live tests (`tests/live`, marker `live`) run against real OpenAI
and Gemini keys and are opt-in; everything else uses fake providers.

## Personas

| Persona | Situation | What they want |
| --- | --- | --- |
| **Priya, solo developer** | A free Gemini key and a paid OpenAI key in one `.env`; one side project. | Free Gemini first, OpenAI only as a capped backup, without changing her OpenAI SDK code. |
| **Marco, multi-project developer** | Three apps with different budgets and keys. | One profile per app (`qm/blog-bot`, `qm/support`), separate paid rules, usage per day and provider. |
| **Sam, coding-tool user** | Uses OpenCode and has spare free-tier capacity. | Point the tool at one local endpoint and keep provider keys out of its config. |
| **Dana, debugging a fallback** | A request was slow or served by the backup. | See which target served it, which were skipped and why, without logs of her prompts. |

## Stories and acceptance criteria

**S1 - Import keys from a `.env` file** (Priya)
- The preview lists recognized names, provider and a suggested plan (Gemini FREE, OpenAI
  PAID); it flags placeholders and duplicates and never returns a value.
- She confirms each plan; imported keys appear in the wallet under FREE and PAID.
- With "reference" storage the database holds only variable names, never key values.

**S2 - See which models a key can use** (Priya)
- Doctor lists the key's models with one free listing call and no generation.
- Gemini ids appear without the `models/` prefix; unusable ids are counted, not shown.
- A successful listing is labeled as listing access only, not proof of quota.

**S3 - Use free Gemini as the default** (Priya)
- Setup saves `qm/default` from a key and model with paid access off and no generation.
- Non-stream and stream requests succeed with `X-QuotaMesh-*` route headers.
- Streams end cleanly, carry no error event and no choices-less chunk, and their token
  usage is recorded even though the client did not ask for it.

**S4 - Keep existing OpenAI SDK code** (Priya, Marco)
- Changing only `base_url`, `api_key` and `model` works for `models.list()`, `create`,
  streaming, tool calling with a local function round-trip and `response_format`
  `json_object`; raw responses expose the route headers.

**S5 - Never spend by accident** (Priya)
- A profile whose only targets are PAID or UNKNOWN keys answers `403 paid_blocked`, for
  stream and non-stream, with **zero** provider calls.
- The route preview shows `paid_blocked` with a link to the setting that fixes it.

**S6 - Cap a paid backup** (Priya)
- With paid allowed, a daily cap and manual prices, each paid call is priced locally from
  the provider's usage and counted against the cap.
- Once observed spend reaches the cap, the next request is blocked (`cap_reached`) before
  any provider call, and the response names the profile, request id and reason.

**S7 - Survive a rejected key** (Dana)
- A rejected key (Gemini's array-shaped 400) is classified as an auth failure and marked
  INVALID; the next key in the pool serves the same request (`attempts 2`, `fallback true`).

**S8 - Fall back across providers** (Dana, Marco)
- An unknown model on target 1 (Gemini 404) moves to target 2 (capped, paid OpenAI) in
  the same request; the trace shows `model_access` then OK with a priced attempt.

**S9 - Prove one target can answer** (Priya)
- Doctor generation needs explicit consent, tests exactly one saved target under the
  profile's paid rules, never falls back, and reports metadata only (finish reason,
  output tokens, reply length).

**S10 - See usage accumulate** (Marco, Dana)
- The wallet counts today's routed requests, attempts, tokens and known cost; a 14-day
  daily series splits them by provider.
- Activity lists each request; `/api/requests/{id}` shows its ordered attempts. No prompt
  or reply text appears anywhere in history.

**S11 - Concurrent requests** (Marco)
- Parallel requests are all served and each is recorded with its own attempts.

**S12 - Connect a coding tool** (Sam)
- Connect generates an OpenCode configuration that uses the OpenAI-compatible transport,
  the selected `qm/<slug>` alias and an environment reference for the local key.

**S13 - Get back into the dashboard** (any persona)
- An already used or expired sign-in link shows a page that says to run `quotamesh open`,
  which prints or opens a fresh one-time link.

## Story-to-test map

| Story | Live test (`tests/live/test_live_user_stories.py`) | Unit and browser tests | Manual path |
| --- | --- | --- | --- |
| S1 `.env` import | `test_s1_import_env_file_as_references` | `tests/test_envfile.py`; `test_phase_four.py::test_environment_is_server_only_reference_idempotent_and_unknown`; `test_phase_five.py::test_duplicate_create_rotation_and_environment_are_safe` | Get started: paste `.env`, confirm plans; Overview |
| S2 Model listing | `test_s2_doctor_lists_models[gemini/openai]` | `test_phase_four.py::test_doctor_listing_classifies_without_spending_or_leaking`, `::test_doctor_bounded_body_and_network_failure`; browser smoke Doctor listing | Get started model picker; Help, Diagnostics |
| S3 Gemini default | `test_s3_gemini_through_default_profile` | `test_gateway.py::test_bootstrap_setup_and_nonstream_passthrough`, `::test_stream_commit_and_first_event_error`; `test_phase_five.py::test_initial_setup_is_authorized_atomic_and_paid_safe`; browser smoke first-run setup | Get started, then Playground |
| S4 SDK compatibility | `test_s4_openai_sdk_compatibility` | `test_phase_four.py::test_snippets_are_parseable_and_use_profile_alias_and_secret_references`; `scripts/integration_smoke.py` (installed Python/Node SDKs, shell clients); [`examples/`](../examples/README.md) | Connect, then your own app |
| S5 Paid guard | `test_s5_paid_guard_makes_no_upstream_call` | `test_gateway.py::test_paid_blocked_until_explicitly_enabled`; `test_phase_two.py::test_paid_default_unknown_price_expiry_and_attempt_limit`; `test_phase_three.py::test_minimal_add_needs_no_model_or_label_and_does_not_enable_paid`; browser smoke paid-guard scenario | Route preview; Profiles (paid setting) |
| S6 Paid cap | `test_s6_paid_cap_prices_then_blocks` | `test_phase_two.py::test_stream_usage_cost_paid_cap_and_persistence`, `::test_paid_unknown_cost_blocks_next_request_with_cap_and_override_works`; `test_phase_four.py::test_generation_paid_cost_and_cap_are_durable`; browser smoke paid-cap scenario | Profiles (cap, prices); Activity |
| S7 Rejected key | `test_s7_rejected_gemini_key_falls_back` | `test_phase_two.py::test_retryable_key_failure_falls_back`, `::test_gemini_body_failure_persists_correct_scope`; `test_phase_three.py::test_late_old_secret_failure_cannot_invalidate_rotated_key`; browser smoke rejected-key scenario | Playground, then Activity (expand trace); API access |
| S8 Cross-provider fallback | `test_s8_unknown_model_falls_back_to_paid_openai` | `test_phase_two.py::test_provider_model_errors_move_to_next_target_without_invalidating_key`, `::test_404_skips_same_model_pool_and_moves_target` | Route preview, Playground, Activity |
| S9 Doctor generation | deferred | `test_phase_four.py::test_generation_requires_consent_obeys_paid_rules_and_never_falls_back`; browser smoke Doctor paid guard | Help, Diagnostics |
| S10 Usage and history | `test_s10_usage_and_activity_accumulate` | `test_phase_three.py::test_fallback_activity_distinguishes_requests_and_attempts_and_is_private`, `::test_activity_cursor_handles_interleaved_traces_and_retention_preserves_rollups`; `test_phase_four.py::test_feed_limits_overflow_and_metadata_only` | Overview; Activity |
| S11 Concurrency | deferred | `test_phase_two.py::test_parallel_calls_recompute_cooldown_before_selection` | Activity |
| S12 Coding tool | none (no live tool run) | `test_phase_four.py::test_snippets_are_parseable_and_use_profile_alias_and_secret_references`; `scripts/integration_smoke.py` OpenCode-compatible transport | Connect, OpenCode tab |
| S13 Sign-in recovery | none | session-link tests from the live-readiness platform work | Run `quotamesh open` |

Live tests marked "deferred" are planned but not part of the current suite. The live
suite's docstrings name the integrated features each story depends on.

## Running the live stories

```sh
python -m pytest -m live tests/live -q --tb=short -p no:cacheprovider
python -m pytest -m live tests/live -q -k "s5 or s2"   # stories without generation calls
```

Keys come from the checkout's `.env` (or `QM_ENV_FILE`) and are saved only as environment
references in temporary data directories that are deleted afterwards. Every chat request
must carry a small token cap, Gemini calls are spaced five seconds apart, and each run stops
at `QM_LIVE_MAX_OPENAI` / `QM_LIVE_MAX_GEMINI` chat calls (defaults 40 and 30). The run
ends with a one-line budget summary. See the [README](../README.md#development-and-tests)
for costs and model overrides.
