# Final MVP architecture (v0.1.0)

One local FastAPI/Uvicorn process owns the gateway, dashboard, shared HTTPX client, and
SQLite store. No service dependencies or frontend build. Named project profiles contain ordered provider/model targets with optional pinned keys.
`qm/default` remains compatible; `qm/<slug>` resolves the corresponding profile.

## Functional core and shell

- `engine/select.py` enumerates eligibility and skip reasons from explicit snapshots.
  It performs no network, environment, database, or clock reads.
- `engine/classify.py` inspects transient status/header/body evidence and calculates
  credential, quota, model-access, or temporary-provider outcomes. Error text is not stored.
- `routing.py` resolves secrets' presence and current persisted/runtime state, then invokes
  that same selector for live routing, status, and dry runs. Paid-accounting failure appears
  consistently in all three paths.
- `routes/gateway.py` authenticates, parses raw request JSON, recomputes candidates before
  every upstream call, changes only model, and handles bounded attempts/deadlines. Generic
  bad requests are terminal; context-length failures move to the next configured target.
- `routes/dashboard.py` validates local configuration and offers the browser management and
  explicit test surface. `/api/test` invokes the same gateway handler without exposing the
  generated bearer key in HTML. Foreign-origin writes are rejected before that handler.
- `routes/wallet.py` and `credentials.py` provide strict profile/credential CRUD, write-only
  rotation and metadata usage/history APIs. `wallet.py` groups shared ownership, expiry,
  allocations and observed state without probes or fabricated quota. `usage.py` aggregates
  attempts into per-day/profile/key/provider/model/plan rollups with cost evidence counts.
- `store.py` owns short SQLite transactions, versioned additive migration, write-only
  secrets, metadata attempts, persistent quota state, and durable daily paid spend.
- `ui/` renders nine task pages with shared helpers and a current-page JavaScript module;
  no third-party assets or direct provider calls. The home wallet leads with owned
  capacity; profile context follows navigation. Independent panel reads provide retry
  and stale feedback, and draft navigation is guarded. Content-digest asset URLs avoid
  stale scripts. Test response text is transient browser output, not database history.
- `demo.py` supplies synthetic error/response scenarios. The CLI's demo uses a temporary
  store and mounted loopback fake upstream; scenario resets cannot touch normal user data.

## State boundaries

| State | Scope | Lifetime |
| --- | --- | --- |
| Invalid / unusable credential | Concrete credential | SQLite; explicit recovery |
| Cooldown / evidenced exhaustion | Provider + quota group + model | SQLite; reset timestamp or manual reset |
| Model-access 403 | Credential + model | Memory; 30 seconds |
| Model-not-found 404 / network / 5xx | Provider + model | Memory; 30 seconds |
| Paid caps | Selected profile, UTC day/month | Durable daily rollups; independent of history |
| Accounting incomplete | Process + safety marker | Paid fails closed across restart |

Default grouping is conservative, except Groq's separate project-specific key defaults
from the PDF. Users must identify shared project boundaries; labels cannot create capacity.
A late success does not clear a still-active shared cooldown from an overlapping request.
After recovery, a successful request resets the strike ladder.

Schema v3 preserves phase-one/two credentials, targets, attempts and existing paid totals. Legacy paid costs
without evidence are marked unknown. Daily paid accounting is updated in the same
transaction as its attempt row and detailed usage rollup. Missing usage/cost is never silently zeroed; known rejected
provider requests carry a local zero-cost inference, while uncertain transport/protocol
outcomes remain unknown. Declared-free success can also infer local zero, never a provider
measurement. Source counts distinguish missing USD observations from explicit reported zero.

## Identity, ownership and history

Profiles have immutable unique slugs, including archived ones. Their numeric identities
and paid totals remain after deletion. Default cannot be deleted. Credential deletion
removes the secret record and archives metadata, preventing ID reuse; pins guard deletion
and provider changes. Editing metadata keeps the secret unless rotation is explicit.
A credential revision check prevents an old in-flight 401 from invalidating a rotated key.
Rotation resets credential invalidity without clearing shared quota or spend.

Wallet ownership is provider + quota group, with conservative defaults. Starting credit
is taken once for a shared group; conflicting manual values, unknown costs or incomplete
legacy coverage prevent a numeric remaining estimate. Archived siblings' observed costs
remain included in their group. Provider changes retain historical rollups without
attributing another provider's old traffic to the new source. Group/plan edits are manual
reclassification, not proof of independent quota. Outside-gateway usage is invisible.

Request traces use UUIDs and ordered attempts with captured profile/key labels and plan
metadata. Cursors paginate complete request groups even when concurrent attempts interleave.
History is limited to 30 days/50,000 rows; daily rollups remain. Upgrades backfill retained
v1/v2 detail once and explicitly label missing earlier coverage. Pure API rejects/no-capacity
responses without upstream attempts do not increment routed-request observation counts.

## Streaming and cost

The first valid SSE response event (including role-only) is the commitment point. Comments
are retained but do not commit; error/malformed-first events can fall back. After commitment,
raw bytes remain on that provider, interruption is an SSE error, and cancellation closes the
upstream. Usage inspection is bounded to small events and only stores token/cost metadata.
No unsupported stream-usage field is injected. Missing `[DONE]` is a failure.

Caps check observed local spend before selection. In-flight final cost can overshoot; no
provider-side billing guarantee is claimed. With a cap, unknown price/spend blocks paid
routing unless explicitly overridden. Manual token prices require provider usage to estimate
cost. An explicit USD cost field takes precedence over local estimates. Arbitrary credits or
undocumented cost units are not converted to dollars.

## Explain/Connect and diagnostics

`routing.runtime_snapshot` reads policy, targets, quota state, spend and one evaluation time.
`connect.explain` decorates those exact candidate decisions with explanations and cap
evidence; raw credential records never enter its public response. Recovery means the
earliest known timed block, not guaranteed eligibility once another policy condition applies.

Connection templates use the actual local origin plus `/v1` and `qm/<slug>`. Keys stay in
client environment references. OpenCode uses the Chat Completions-compatible SDK transport.
Discovered upstream models are suggestions only; the public models endpoint advertises
callable profile aliases, never arbitrary discovered IDs that bypass profile policy.

Doctor runs only after an authenticated action. Listing is one bounded request (10 seconds,
1 MiB, up to 1,000 printable IDs); it follows no redirects and never falls back to generation.
Listing-level rate limits do not create generation-model cooldowns. Explicit generation
narrows the existing gateway pipeline to one saved target/key and one upstream attempt,
with the same caps, state transitions and metadata accounting. Browser cancellation stops
the diagnostic task and closes upstream work. No completion/error body enters diagnostics.

Schema v4 transactionally adds the latest check per credential/mode, including timestamps,
sanitized outcomes, latency, discovered IDs and optional request ID. Revision/endpoint guards
prevent old checks from overwriting rotated or deleted credentials. Rotation/provider/endpoint
changes clear discovery. Observations become visibly stale after 24 hours; they never promise quota.

Environment import inspects an allowlist in the server process, returns presence and names
only, and stores references as UNKNOWN unless a plan is explicitly supplied. No file scanning,
provider call, target creation or paid permission accompanies import. The Access Catalog is
dated link/notes data, independent of candidate selection and pricing.

`ActivityFeed` publishes metadata only after attempt persistence. Up to 64 subscribers have
64-item queues; overflow sends a refresh hint. SSE reconnect requests a fresh snapshot,
disconnects clean up subscribers, and keepalives make no provider calls. Frontend refreshes
preserve draft targets and reject stale responses after profile switches.

## Trust boundaries and limits

The loopback, Host/origin, bootstrap/session, local bearer, HTTPS endpoint, redirect, bounded
request body, and metadata-only privacy boundaries remain from phase one. SQLite secrets
are plaintext with best-effort filesystem restrictions; environment references avoid storing
raw provider keys in SQLite. No background probes, telemetry, or body logging.

Only Chat Completions is supported. Custom/preset API behavior is synthetic-fixture tested;
live account/model verification requires the user's authorized keys. See the
[phase-four implementation](phase-four-plan.md) for verification and assumptions, and the
[roadmap](../ROADMAP.md) for phase boundaries.

## Final release additions

Schema v5 adds nullable first-response timing to attempts/rollups and bounded,
allowlisted rate observations guarded by credential revision and endpoint. These
are passive metadata only, stale after at most 60 seconds or an earlier reset.
Initial setup atomically creates default with paid off; existing target replacement
requires normal profile editing. Duplicate access is rejected at add/rotation/import;
legacy effective-key aliases share observed blocks and are never retried in one
request. OS-held process locking protects each data directory during app lifespan.

The frontend caches static catalog/snippet data and preserves expandable evidence
and traces. No new runtime dependencies were introduced. The MIT 0.1.0 distributions
are verified through real uvx processes outside the checkout. Platform CI and OIDC
publishing are separate from live-provider readiness. The final testing guide records
MVP coverage and documented exclusions; release evidence records actual results.
