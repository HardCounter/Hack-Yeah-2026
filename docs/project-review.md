# Repository review and verification record

Review date: **2026-10-06**. Baseline: latest fetched `origin/main`,
`ed7df113` (merge of pull request #20). Changes are on `improve/project-quality`.
The working tree was clean before switching branches. No release, remote push, infrastructure
apply, provider call or external customer operation was performed.

## Assessment

This is a substantial team engineering demonstration: an external Python policy gateway,
transactional SQLite evidence/outbox pipeline, plugin-based supervision, independent business-state
verification, an OpenCode adapter, operator dashboard, and reproducible synthetic KYC workflow.
The strongest evidence is the executable path from admission to persisted effects and independent
verification, backed by fault, concurrency, integration and end-to-end tests.

It is suitable to present as a **team-built AI agent control-layer prototype**. Attribute individual
contributions separately using actual contribution history. Avoid claims of production security,
live banking/AML integration, universal prompt-injection protection, or complete runtime interception.
An agent's own success message is deliberately insufficient to establish task success.

The baseline inventory contains 324 tracked files, including 182 Python and 68 Markdown files.
The review covered the tracked repository inventory, challenge Rules/Criteria and project direction,
shared contracts/configuration, interception and adapter, persistence and consumers, synthetic data
and runners, web/frontend, CI/container/infrastructure, and supporting documentation. Three read-only
GPT-6 Luna reviewers examined runtime/security, persistence/supervision, and presentation/operations;
their findings and a second diff review informed the changes. The builder implemented and verified
the changes. This is an engineering review, not a penetration test, legal audit or formal proof.

## Findings addressed

| Area | Finding | Result on this branch |
|---|---|---|
| OpenCode model admission | Message deltas missed edits with unchanged message counts and undercounted repeated context | Enforcement forwards full normalized history and system text on every request; observe mode retains deltas |
| Inspection completeness | Clipped text, omitted results and unsupported media could be admitted without inspecting their full content | Explicit incomplete-content markers fail closed in Python admission; unsupported media is marked omitted; private reasoning remains excluded |
| System instructions | Forwarded system text was absent from Python prompt scanning and metering | System content is validated and included before request admission |
| Audit immutability | Bound-writer retries compared only action/tool identity | Retries reconstruct trusted scope and compare the complete sanitized durable projection, reusing only store-assigned timestamp/order |
| Read consistency | Retention could remove rows between pages of a scoped audit read | Atomic snapshot/hold creation, scope-bound expiring holds, refresh on continuation and release on final page |
| Storage capacity | Separate content bodies were outside event byte accounting; lower-level insertion paths could bypass quota checks | Content and event UTF-8 bytes share a transactional quota; failed appends roll back and identical retries do not consume bytes twice |
| Content ingestion | Single content bodies and disk headroom lacked equivalent limits | Per-body bound, deduplication, aggregate quota and minimum free-space check |
| Plugin diagnostics | Raw exception messages could reach retry/ledger or manager lifecycle logs | Exception types or fixed failure codes replace raw messages in imports, handle, commit, setup, teardown and manager failure paths; import exception chains suppressed |
| Local session privacy | Permissive umasks or existing directories exposed runtime storage/logs | Session and shared evidence directories use 0700 and process logs use 0600 on POSIX; existing roots are tightened |
| Domain configuration | Malformed DNS labels could pass blocklist validation | Label length, characters, edge hyphens and total domain length are checked |
| Case coverage | OpenCode CLI rejected applications 16 and 17 despite dataset support | Application range accepts all 17 generated cases |
| Python correctness | Shadowed definitions/test names, duplicate imports, an undefined annotation and a confusing bait-tool import | Stale definitions/imports removed, annotation fixed and registration import named explicitly; focused Ruff checks added |
| Dashboard interpretation | Approval-required actions were described as executed high-risk approvals | Labels and drilldown identify approval-required execution denial while approval workflow is unavailable |
| Dashboard demonstration | Main setup depended on Docker/provider configuration | New loopback offline dashboard generates actual governed evidence and disables live sessions/config writes |
| Suite lifecycle | Collection and replaced test reports left temporary workspaces behind | TemporaryDirectory cleanup for collection and previous completed reports |
| Build/release confidence | CI tested code without building the deployed image | Added Compose validation/image build job and pre-SSH deployment artifact checks; workflow permissions reduced to contents read |
| Reproducibility | Docker copied uv from `latest` | uv pinned to verified version tag 0.11.29 |
| Container context | Generated reports/runtime output and alternate local environment files were not excluded | Build context excludes reports, runs, Ruff cache and `.env.*` while retaining `.env.example` |
| Infrastructure consistency | Bootstrap cloned the default branch while release/runbook expected `deploy` | Terraform bootstrap clones the deployment branch explicitly |
| Project entry point | Setup, capabilities and historical claims were difficult to assess | README rewritten around working commands, architecture and limits; CONTRIBUTING, SECURITY and dependency inventory added |
| Historical documentation | Deployment notes overstated active semantic checks and understated current fixture-backed OpenCode tests | Current behavior clarified; dated design/results remain historical rather than blanket capability claims |

Behavior changes have regression coverage for incomplete prompts/system inspection, same-count
context edits, retry conflicts, retention interleaving, quota rollback/deduplication, private diagnostics,
permissions, domain validation, all CLI cases, report cleanup and the offline dashboard's API boundaries.

## Verified locally

Environment: macOS, development Python 3.12, uv 0.11.29, Node.js 26.10.0, and OpenCode 2.0.22.
CI specifies Node.js 22 and tests Linux, Windows and macOS. Local checks do not establish that the
new CI jobs have run successfully on those systems.

| Check | Command / method | Result |
|---|---|---|
| Locked installation | `uv sync --locked` and `uv lock --check` | Passed |
| Python correctness | `uv run --locked ruff check .` | Passed; E9, F63, F7, F82 and F811 rules |
| Baseline Python suite | `uv run --locked pytest -q` before edits | 797 passed, 2 skipped, 44 subtests passed |
| Final Python suite | `uv run --locked pytest -q` | 821 passed, 2 skipped, 49 subtests passed in 77.51 seconds |
| Adapter and frontend | `node --experimental-vm-modules --test tests/frontend/dashboard.test.mjs adapters/opencode/test.mjs adapters/opencode/forward.test.mjs` | 26 passed, 0 skipped |
| Dataset | `uv run --locked python data/generate.py` | Self-check and determinism passed; 150 clients, 231 accounts, 9,354 transactions, 60 alerts, 17 applications, 47 documents |
| Dataset report | `uv run --locked python data/report.py` | Rendered successfully |
| Positive quickstart | `CONTROL_LOG=null uv run --locked python simulation/agent.py APP-0001 --driver scripted` | Exit 0, VERIFIED_SUCCESS; screening/effect provenance and business-state checks passed |
| Negative quickstart | Same CLI for APP-0003 with `--fault skip_step:screen_sanctions` | Expected exit 1; create_client BLOCK, application remains new, VERIFICATION_INCOMPLETE |
| Offline dashboard HTTP | `python -m web.demo --port 18888`, real loopback GETs | `/`, `/healthz`, `/api/v1/health`, sessions and security metrics all returned 200; generated evidence, no provider |
| Offline shutdown | Separate real subprocess served sessions, then received SIGTERM | HTTP 200, exit 0, temporary evidence/config workspace removed |
| Dashboard integration | Part of full Python suite | Actual HTTP applications, governed SQLite evidence and shipped JavaScript exercised using Node |
| Real OpenCode integration | Part of full Python suite with CLI installed | CLI loading/handshake and governed tool pipelines use a loopback fixture model; not hosted-provider inference |
| Compose | `docker compose ... config --quiet` with a temporary copy of `.env.example`, Compose and Caddy files | Passed without reading a real environment file |
| Image build | `docker build --build-arg GIT_SHA=working-tree --tag sentinel-interlock:review .` | Passed on linux/arm64 after starting the installed Docker Desktop |
| Container demonstration | `docker run --rm --env CONTROL_LOG=null sentinel-interlock:review scripts/run_demo.sh APP-0001` | Exit 0, VERIFIED_SUCCESS |
| Container/proxy integration | Disposable Compose project using the built image and `.env.example`; loopback ports and separate subnet | App/API healthy; Caddy returned 200 for dashboard, health, console, configuration and security metrics; two generated governed sessions read through shared storage |
| Workflow configuration | YAML parsing and source review | Passed parsing; remote execution not performed |
| uv image version | Docker registry manifest inspection | `ghcr.io/astral-sh/uv:0.11.29` exists with four platform entries |
| Patch hygiene | `git diff --check` | Passed |

The two Python skips are optional hosted-provider tests. Real OpenCode fixture-backed tests ran
locally; they were not silently counted as passing skips. Generated data, reports and run artifacts
remain Git-ignored. No credentials were read or added to tracked files.

## Remaining boundaries and decisions

| Priority | Boundary / gap | Practical next step |
|---|---|---|
| Before public deployment | Evidence reads and session/test endpoints lack general user/tenant authentication; session URLs are bearer capabilities and rate limits are process-local | Put the demonstration behind an access gate; add identity/tenant authorization and shared quota controls for a product deployment |
| Before stronger runtime claims | Cooperative adapter coverage is not an OS/network sandbox; real prompt-hook throw propagation and final-answer interception are incomplete evidence | Add real-runtime failure-injection tests and a complete event/completion integration for the pinned CLI |
| Before handling real data | Child environment forwarding is broader than a provider credential allowlist; trusted plugins can execute arbitrary code | Define narrow environment/capability boundaries and a stronger process isolation model |
| Before long-lived storage | Maintenance is not automatically scheduled and does not comprehensively prune content/newer governance tables | Define ownership/retention for every table and content body, schedule maintenance, monitor physical disk growth |
| Before human approval claims | REQUIRE_APPROVAL denies execution; there is no interactive approval queue | Implement authenticated approvals, expiry, durable decisions and bound resumption |
| Before semantic-control claims | Layer 1 semantic-guard settings are reserved; only the optional consume-plane judge is implemented | Either implement/test that control or continue labeling the settings as reserved |
| Before outcome guarantees | Independent verification detects wrong state after a side effect; it does not roll back effects, and delivery is at least once | Define recovery/compensation and downstream idempotency for each real integration |
| Before risk-performance claims | Trajectory expected loss/noisy-OR outputs are configured heuristics | Evaluate calibration, false positives and detection performance on a defined dataset |
| Before complete dashboard/API claims | Some run/case/agent trajectory scopes and timeseries endpoints are deferred and return 501 | Keep unavailable states explicit; implement endpoints only with actual evidence sources |
| Before infrastructure use | Terraform demo SSH ingress defaults to all IPv4 sources | Restrict access through an owner-controlled tunnel or deliberate CIDR policy; hosted CI SSH release access must be accounted for |
| Before reproducible redistribution | Base images remain mutable version tags and the CLI's transitive npm dependency graph is not repository-locked | Pin image digests and capture/review the complete CLI/system dependency graph |
| Maintainer decision | No license for the team's own source; dependency inventory is metadata, not a complete notice/legal review | Agree project redistribution terms with the team and preserve required upstream notices |

The temporary Compose project used its own volumes/network, which were removed after testing.
These items were documented rather than represented as implemented. Adding authentication,
approval workflows, complete semantic enforcement or arbitrary external integrations would be
new product scope, not a safe incidental cleanup of this demonstration.

## Checks not established

- Docker build and Compose checks ran on local linux/arm64. Remote deployment, linux/amd64 image
  execution, TLS issuance and hosted-provider operation were not verified. The new CI build job
  has not yet run remotely on this branch.
- Terraform was unavailable. No Terraform validation, plan, apply or AWS connectivity test ran.
- The hosted health check timed out in the review environment; current availability was not
  established. A timeout alone does not prove that the deployment is down.
- No hosted model tests or external banking/MCP service tests ran.
- Dashboard behavior was exercised through HTTP and the DOM integration harness. No manual browser
  visual/accessibility audit was possible in this environment.
- No project-wide dependency vulnerability scan, comprehensive license audit, performance benchmark,
  formal security analysis or production load test was performed.

The dated test record supports the implemented prototype scope. It should not be reused as proof
of a future revision or deployment without rerunning its relevant checks.
