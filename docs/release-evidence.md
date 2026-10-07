# QuotaMesh 0.1.0 release evidence

Validated on 2026-10-07. This report distinguishes repeatable local acceptance,
GitHub platform checks, public publication, and live-provider claims.

## Local acceptance

| Check | Actual result |
| --- | --- |
| Ruff and JavaScript syntax | Passed |
| Full pytest suite, Windows / Python 3.14 | 135 passed |
| Wheel and source distribution | Built successfully; MIT license included |
| Clean uvx resolution and real installed processes | Passed, no editable checkout; first fake-provider request in 3.56 seconds |
| Routing and two independent keys on one provider | Primary 429, backup 200; two attempts; passed |
| Streaming / assets / authentication | Passed |
| Second owner and graceful restart | Second owner rejected; restart preserves history/cooldown; passed |
| Synthetic privacy checks | No test prompts, completions, or provider secrets in activity/wallet/process logs |
| Chromium desktop and 390px mobile | Passed, no horizontal overflow, zero JavaScript errors |
| Browser workflows | First-run setup, credential/profile CRUD, rotation, wallet, six failure scenarios, Explain/Connect/Doctor passed |
| Installed Python SDK | Chat Completions and streaming passed |
| Installed Node SDK and generated shell clients | Node OpenAI, Bash/cURL, PowerShell passed |
| OpenCode-compatible transport | Passed through its compatible provider adapter; full OpenCode application not tested |
| Final guide | Six-page PDF generated from the maintained Markdown guide and visually reviewed |

The timing above measures the local cached-wheel acceptance path against a fake
provider. It does not predict download time, provider latency, or real-account quota.
All automated request tests use synthetic data and a loopback fake provider.

## Cross-platform and publication evidence

The [CI workflow](https://github.com/yashsrivastava0/QuotaMesh/actions/workflows/ci.yml)
runs Windows/macOS/Linux on Python 3.11 and 3.14, plus Linux on Python 3.12/3.13.
Each platform builds and runs installed-wheel acceptance. Browser and generated
client acceptance are separate jobs. A merge is permitted only after those checks
pass; use the run attached to the Phase 5 pull request/main commit for final results.

The [release workflow](https://github.com/yashsrivastava0/QuotaMesh/actions/workflows/release.yml)
repeats quality checks, verifies the exact main/tag/version identity, validates
distributions, and publishes only when explicitly dispatched with publication
enabled. Its post-publication jobs verify clean PyPI installation on all three OSes.
The [GitHub releases page](https://github.com/yashsrivastava0/QuotaMesh/releases)
contains distribution, checksum, guide, and synthetic walkthrough artifacts.

Public PyPI availability must be established by a successful publishing run and
clean index installation. Creating a GitHub release or building a wheel alone does
not establish it. PyPI account ownership and a matching Trusted Publisher are
external prerequisites. Until public availability is verified, install the supplied
wheel using the instructions in the final testing guide.

## Product boundaries

The complete final MVP checklist is mapped in [final-testing-guide.md](final-testing-guide.md).
Phase 5 closes the PDF MVP; there is no planned Phase 6. Native Responses/Messages,
encrypted/keyring storage, hosted/team access, model ranking, and non-chat APIs
remain outside the accepted MVP. Directly saved secrets remain plaintext in local
SQLite; Windows POSIX permissions are not equivalent to a reviewed Windows ACL.

Provider presets and dated official links establish configuration intent, not a
live credential or entitlement. No real provider account was charged or probed for
these acceptance runs. Passive rate snapshots expire; unknown values stay unknown.
