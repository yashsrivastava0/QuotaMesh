# Phase two architecture

One local FastAPI/Uvicorn process owns the gateway, dashboard, shared HTTPX client, and
SQLite store. No service dependencies or frontend build. One default profile contains
ordered provider/model targets with optional pinned keys; named profiles are phase three.

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
- `store.py` owns short SQLite transactions, versioned additive migration, write-only
  secrets, metadata attempts, persistent quota state, and durable daily paid spend.
- `ui/` renders a route lab with small local JavaScript; no third-party assets or direct
  provider calls. Test response text is transient browser output, not database history.
- `demo.py` supplies synthetic error/response scenarios. The CLI's demo uses a temporary
  store and mounted loopback fake upstream; scenario resets cannot touch normal user data.

## State boundaries

| State | Scope | Lifetime |
| --- | --- | --- |
| Invalid / unusable credential | Concrete credential | SQLite; explicit recovery |
| Cooldown / evidenced exhaustion | Provider + quota group + model | SQLite; reset timestamp or manual reset |
| Model-access 403 | Credential + model | Memory; 30 seconds |
| Model-not-found 404 / network / 5xx | Provider + model | Memory; 30 seconds |
| Paid caps | Default profile, UTC day/month | Durable daily rollups; independent of history |
| Accounting incomplete | Process + safety marker | Paid fails closed across restart |

Default grouping is conservative, except Groq's separate project-specific key defaults
from the PDF. Users must identify shared project boundaries; labels cannot create capacity.
A late success does not clear a still-active shared cooldown from an overlapping request.
After recovery, a successful request resets the strike ladder.

The migration preserves phase-one credentials, targets, and attempts. Legacy paid costs
without evidence are marked unknown. Daily paid accounting is updated in the same
transaction as its attempt row. Missing usage/cost is never silently zeroed; known rejected
provider requests carry zero observed cost, while uncertain transport/protocol outcomes
are treated conservatively. Full wallet rollups are deliberately deferred.

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

## Trust boundaries and limits

The loopback, Host/origin, bootstrap/session, local bearer, HTTPS endpoint, redirect, bounded
request body, and metadata-only privacy boundaries remain from phase one. SQLite secrets
are plaintext with best-effort filesystem restrictions; environment references avoid storing
raw provider keys in SQLite. No background probes, telemetry, or body logging.

Only Chat Completions is supported. Custom/preset API behavior is synthetic-fixture tested;
live account/model verification requires the user's authorized keys. See the
[implementation plan](phase-two-plan.md) for research and assumptions, and the
[roadmap](../ROADMAP.md) for phase boundaries.
