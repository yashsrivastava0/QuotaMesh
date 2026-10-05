# QuotaMesh product roadmap

Source: `QuotaMesh_MVP_Architecture_Product_Spec_v0.3.pdf` (all 65 pages reviewed). The spec's six implementation stages are consolidated into five deliverable phases. Each phase depends on the preceding one; no later phase is needed to run the Phase 1 gateway.

| Phase | Deliverable | Depends on | Exit criterion |
| --- | --- | --- | --- |
| **1. Secure local pass-through** | Python package, versioned SQLite store, dated provider registry, fake upstream, loopback-only FastAPI app, bootstrap session, local gateway bearer key, a setup/status web UI, one `qm/default` target, generic Chat Completions transport (stream and non-stream), metadata-only attempts, CLI and tests. | None. | A new user can configure one authorized credential and model, then make a Chat Completions request through localhost with the standard OpenAI API shape. |
| **2. Deterministic decision engine** | Pure candidate selector shared by traffic and dry runs; ordered target pools; quota-group/model cooldown persistence; body-aware provider classifiers; retry budget/deadlines; expiry and paid-cap enforcement; pre-commit fallback tests. | Phase 1 store, transport, fake upstream. | The spec's failure and paid-safety integration matrix passes. |
| **3. Capacity Wallet and Project Profiles** | Full credential management; FREE/TRIAL/PAID wallet; named ordered profiles; local daily usage rollups, honest truth badges, and activity history. | Phase 2 decisions and accounting. | A mixed-capacity user can understand ownership and allocate sources to projects without reading logs. |
| **4. Explain and connect** | Available Now/Explain Route from the same candidate function; integration snippets; on-demand Doctor; scripted demo; small dated Access Catalog. | Phase 3 profile UI; Phase 2 selector. | A user can inspect policy, understand skips, and connect a project in minutes. |
| **5. Release and post-MVP evolution** | Packaging/release QA and cross-platform smoke checks first; then separately gated local secret polish, richer capabilities, Responses/Messages compatibility, and broader inference endpoints when justified. | MVP acceptance from Phases 1–4. | `uvx quotamesh start` and the spec's release checklist pass before optional extensions ship. |

## Why Phase 1 comes first

The gateway is the product's trust boundary. Auth, secret handling, raw request preservation, streaming commitment, and metadata-only persistence must work before the selector or wallet can safely depend on them. A thin setup UI makes this a usable vertical slice rather than an engine without a user path. The fake upstream allows verification without consuming provider quota.

## Phase 1 assumptions and boundaries

- The repository initially contained only the spec PDF and `.codex/config.toml`; there was no source, README, `AGENTS.md`, or Git repository to preserve or extend.
- Phase 1 supports one default profile target and one configured credential at a time. Its model is entered by the user; no static model catalog or discovery is used. Full profiles, pools, automatic fallback, cooldowns, caps, wallet/accounting views, Doctor, catalog, and protocol translation are later phases.
- OpenAI is the initial preset; a custom OpenAI-compatible HTTPS endpoint (or loopback HTTP endpoint for local testing) uses the same generic transport. Provider-specific quirks and first-class provider claims await tested classifier fixtures.
- Paid and unknown-plan use remain off by default. The setup form requires explicit opt-in if such a credential is to serve requests. Dollar caps are Phase 2, so the UI describes this limit clearly.
- Local attempt metadata covers traffic through QuotaMesh only. No prompt or response bodies are stored. Upstream API secrets are stored in a restricted local SQLite file or referenced from the process environment; native keyring storage is deferred by the spec.
- A first response event is the streaming commitment point. Phase 1 returns pre-commit upstream errors to the client and does not retry another target because target selection is Phase 2.
