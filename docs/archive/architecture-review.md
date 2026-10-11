# Architecture review: October 3, 2026

Scope: repository design and available source, initially at `847079b`. Requested reviewers:
direction-auditor, rules-auditor, verification-auditor and security-auditor. All were invoked
read-only. The direction task returned no substantive report; the security report was
incomplete. The implementation lead completed the missing direction/trust-boundary analysis
from the source documents. This is a design review, **not a runtime security certification**.

The available tools do not expose per-task model selection. GPT 6 Luna was requested but
was **not selected or claimed** for these corrections.

## Material findings and documentation disposition

| Finding in the original design | Correction | Remaining evidence gate |
|---|---|---|
| System architecture built two agents/28 scenarios despite the KYC-only decision in `use-cases.md:7–10`. | KYC is the MVP; AML and broad integrations are deferred; build order prioritizes one controlled write and verifier. | Implement and demonstrate that slice. |
| Trusted Task Contract was optional/incomplete, with ambiguous correlation-header authority (`application-documentation.md:416`, `system-architecture.md:119–131`). | Mandatory external contract, protected approved baseline, authenticated server-side binding; headers confer no authority. | Forged identity/contract/resource tests and backend isolation. |
| Async trajectory feedback could arrive after a harmful write (`application-documentation.md:261–318`). | Selective synchronous pre-action semantic gate; async feedback is an audited later restriction only. | Live AI-control evidence plus required-service failure tests. |
| Reload silently rebound in-flight requests (`application-documentation.md:172–176`). | Validated policy snapshots pinned per run; new versions for new sessions; explicit halt/tightening interventions. | Invalid reload, old/new session and stale approval tests. |
| Approval lacked exact-action/state/replay binding (`application-documentation.md:135`). | Authorized reviewer, canonical argument digest, action/contract/policy/state binding, expiry, single-use consumption and hard-rule rechecks. | Replay, substitution, expiry and state-change tests. |
| Async spend reporting did not establish hard limits; fallback could spend or repeat writes (`application-documentation.md:178–205`, `296–305`). | Protected atomic pre-call reservations including guard calls/retries/local compute; no blind side-effect retry/fallback. | Concurrency, timeout, restart and budget exhaustion tests. |
| Non-blocking in-memory buffering was presented as reliable preservation (`application-documentation.md:209–217`, `system-architecture.md:147–149`). | Durable critical intents, atomic effect receipts, bounded durable analytics outbox and fail-closed write backpressure. | Crash, audit-loss and duplicate-consumer tests. |
| Raw prompts/results/blob retention and output streaming could bypass data controls (`system-architecture.md:108–116`, `253`). | Sanitized allowlisted evidence only; buffered output inspection before agent delivery; private verifier baseline separated. | Output/exception/export leakage and future cross-chunk tests. |
| Trace/ground-truth joins were treated as outcome verification (`system-architecture.md:155–165`); once-per-call was confused with once-per-business-effect (`use-cases.md:175`). | Separate read-only persisted-state checks; unique application/client linkage and atomic receipts; attempts counted separately; explicit schema gaps. | Wrong/missing/extra effects, cross-session duplicates, false-success and verifier-failure tests. |
| Post-run failure was labelled BLOCK, including on a completed write (`dashboard-ui.md:98–105`). | Success/failed-postconditions/incomplete statuses; replay requires a persisted-state snapshot; detection is not prevention/rollback. | Verifier and replay fixture must exist and be executed. |
| Public UI could appear to choose identity/policy and expose shared raw content (`application-documentation.md:469–474`, `dashboard-ui.md:90`, `119`). | Isolated quota-limited sandbox; separate admin/approval authorization; no raw shared evidence or executable regression generation. | Administrative access and hostile-input tests. |
| Python/uv setup was present but undocumented; stale architecture claimed Faker (`system-architecture.md:73`). | README documents uv and empty dependency state; generator identified as stdlib-only. | Lock/environment checks do not establish gateway behavior. |

Corrections are specified in [architecture-contract.md](architecture-contract.md) and the
linked system/application/use-case/dashboard documents. They define requirements; they
do not implement a gateway, policy engine, semantic guard, verifier or dashboard.

## Reviewer recommendations not accepted blindly

- A low-temperature LLM is **not** a deterministic injection scanner or structural verifier.
- Add dependencies to standard project metadata only when selected for actual implementation;
  do not invent `[tool.uv] dependencies` or add redundant `requirements.txt` for an empty project.
- Shared generator/verifier rules provide consistency, not independent rule-correctness proof.
  Hand-authored fixtures are required; verifier logic does not exist yet.
- Scenario counts are not test results, and planned bait tools do not execute real exploits.
- A proxy `base_url`, sidecar or same-user deployment alone cannot establish non-bypassability.
- The dashboard's 2–3 minute/laptop target is a team design choice, not a formal competition rule.
- Team name **HardCounter Team** exists in `pyproject.toml`; the formal member list and
  submission package remain unavailable. No team identities or eligibility facts are invented.

## Submission and implementation blockers

The repository is not submission-ready. Required deliverables still need implementation:
functional intermediary, one documented loadable policy, actual deterministic and live
AI-based controls, budget/signature enforcement, interactive reporting and judge-runnable
positive/negative tests. No security or persisted external-outcome claim is established.

Formal Rules additionally require title/team/member list (1–6), description, a maximum
10-slide PDF and HackTribe submission in English or Polish within the stated event window.
Confirm eligibility/timing with organizers; this review does not establish them. Rules assign
self-testing/scalability **20%/10%**, Criteria **15%/15%**. Keep both unchanged, ask for
clarification and provisionally plan from formal Rules while meeting technical Criteria.
Check licenses of dependencies when actually selected; no license compliance is inferred
from architecture suggestions. Venue/network access remains a practical demo decision.

## Validation scope

For this documentation milestone: inspect the diff and relative links, check Markdown
fences/snippets, run `git diff --check`, validate the existing uv lock, and confirm the
locked offline environment. Report exact executed results separately. No product suite
exists and no control or external business outcome has been runtime-tested in this review.
