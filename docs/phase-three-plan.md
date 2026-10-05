# Phase three: Capacity Wallet and Project Profiles

Task: phase-three. Read all 65 pages of the v0.3 product PDF; repository ROADMAP.md
consolidates the PDF's foundation/pass-through stages into five deliverable phases.
Phase three is the wallet/profile milestone, not the entire MVP.

## Baseline and research

- Phase one/two baseline: 69 tests pass; CI passes on Python 3.11 and 3.12.
- The previously pushed phase-two PR was still a draft. It is integrated into main
  before this branch, using the verified commit and ordinary merge (no force push).
- PDF sections 5, 7, 12, 13, 15, and 17 govern shared ownership, profile allocation,
  truth sources, durable rollups/history, privacy, and acceptance.
- SQLite's official UPSERT and foreign-key documentation informs transactional
  rollups and preventing deletion/provider changes from silently changing pinned
  targets. No ORM or migration framework is needed for this small local store.
- No provider breadth, pricing crawler, background probe, or protocol change is
  needed. Existing generic transport/classifier behavior stays the baseline.

## Checkpoints

1. Add schema v3, named profile persistence, credential lifecycle, and detailed
   daily rollups without discarding v1/v2 data or resetting existing paid caps.
   Verify migration, no ID reuse, secret preservation/rotation, and deletion guards.
2. Resolve qm/<slug> in the gateway, models list, status, dry-run, and test paths.
   Scope budgets and paid accounting to the selected profile; keep shared provider
   quota state across profiles. Test free-only isolation, stream/non-stream routing,
   missing/disabled profiles, restarts, and phase-one/two regressions.
3. Build a wallet-first UI: FREE/TRIAL/PAID groups with shared quota sources,
   credential editing/rotation/disable/delete, named profile CRUD and ordered
   targets, local usage and truth badges, and expandable metadata-only history.
   Exercise the actual browser, including migration and no-secret rendering.
4. Update CLI/docs/roadmap, build and test an installed wheel, push each checkpoint,
   open a PR, wait for required CI, merge to main, and verify the remote main SHA.

## Deliberate choices

- Stay with one process, SQLite, Jinja and small local JavaScript. No new product
  dependencies. Future phase-four views use the existing shared candidate function.
- Keep qm/default working. Named slugs are immutable and unique, including archived
  names; deletion archives identity so historical paid spending cannot be reset by
  recreating a name or reusing a numeric ID. Disable default instead of deleting it.
- Deleting a credential removes its secret and archives non-secret identity. Reject
  deleting a pinned credential; the user must explicitly change those targets first.
  Pool membership is dynamic by provider, as the PDF specifies; show this clearly.
- Metadata edits preserve secrets. Secret/environment-reference rotation resets
  credential invalidity, not shared quota or spend. Provider changes cannot break
  pinned targets. No automatic generation test or implicit billable key probe.
- Count requests distinctly from attempts: fallback is one request with multiple
  attempts. Daily detail is per profile/credential/model/plan; paid caps retain their
  compatible per-profile aggregate. Commit attempt and both rollups atomically.
- History is bounded to 30 days/50,000 rows; pruning never removes daily rollups.
  Backfill retained old attempts once. Old pruned history cannot be reconstructed;
  show legacy coverage limitations and keep existing paid cap totals unchanged.
- PROVIDER, LOCAL, MANUAL, UNKNOWN are explicit source values. Missing token usage
  stays null; measured token totals carry incomplete flags if any usage is missing.
  USD provider cost wins over token-price estimates. Unknown dollars remain unknown.
- Group a provider/quota boundary once even when several keys use it. Show per-model
  states without calling a provider in the background. Never claim exact remaining
  quota. Grouped starting credit must not be summed per key: conflicting inputs or
  any unknown observed cost make estimated remaining credit unknown.
- Monetary starting credit and expiry are manual; remaining credit is a local
  estimate from gateway traffic only. No conversion from abstract provider credits.
- UTC calendar day/month caps remain local safety rails with in-flight overshoot.
  Outside gateway usage is invisible. The UI repeats these limits near balances.
- Doctor/model discovery, a full Explain/Connect screen, generated snippets, and
  the Access Catalog remain phase four; packaging/cross-platform release remains
  phase five. The existing dry-run/test demo is extended only to validate phase three.

## Acceptance

A mixed-capacity user can see owned sources, shared keys, expiry, observed usage,
and unknown values; edit/rotate credentials; create two differently guarded named
profiles; route and stream through their aliases; and read a request's ordered trace
without finding secrets, prompts, or completions in management output/history.
Pruning/restarts/migrations preserve caps and totals. Existing security and all
phase-two failure cases remain tested. No user credential is needed for these tests.

## Verification evidence

- 95 automated tests pass, including the 69 phase-one/two regressions and new v2
  migration/rollback, paid-isolation, named streaming, rotation/restart, shared credit,
  archived identity, complete-history cursor, durable retention and privacy checks.
- Ruff and JavaScript syntax checks pass; source and wheel builds pass.
- Actual Chromium acceptance covers the mixed wallet, shared balance, profile
  create/switch/disable/delete, credential add/edit/rotate/delete, source badges,
  streaming, expandable history, safe HTML rendering, all six phase-two scenarios,
  and a 390-pixel mobile layout. No JavaScript errors were observed.
- A browser check caught early button re-enabling while metadata refresh was still
  pending. The send button now remains disabled until the refreshed view arrives.
- Browser acceptance is in CI alongside the Python 3.11/3.12 test/build matrix.
- An old in-flight credential rejection cannot invalidate a newly rotated secret.
  Cost fields outside the single-request manual-money range remain unknown instead
  of overflowing aggregate JSON. Missing provider USD displays as Not reported.

There are two pending roadmap phases: four and five. No live provider calls or real
credentials were used; account/model readiness remains an explicit user-side check.

The wheel was installed in a separate virtual environment with fresh runtime dependencies
and exercised from outside the checkout. CLI startup, packaged templates/assets, normal
fallback, a named streamed request, shared credit, profile creation, durable usage/history
and safe CLI status all passed. Normal user data was untouched.
