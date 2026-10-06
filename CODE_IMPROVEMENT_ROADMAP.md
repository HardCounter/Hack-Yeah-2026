# Code Improvement Roadmap & Implementation Wishlist

**Project:** Sentinel Interlock — AI Control Layer  
**Date:** 2026-10-05  
**Context:** Comprehensive gap analysis and alignment review between architectural specifications, design documents, and the active codebase.

This document catalogs all capabilities, features, safety mechanisms, and architectural enhancements specified across the project's documentation (`docs/`) including remaining proposals and integration items completed in the codebase. Entries marked **Completed** describe shipped behavior; unmarked proposals remain future work. Items are organized by system plane and prioritized to guide future engineering sprints.

---

## 1. Prioritization & Impact Matrix

| Priority | Plane | Feature / Enhancement | Target Documentation | Primary Benefit |
|---|---|---|---|---|
| **P0** | **Consume** | **Completed:** Fix `trajectory-risk` document ID false positive (`DOC-xxxx`) | `docs/decision-trace.md:162`, `tests/test_decisions_demo.py` | Eliminates false high-risk escalations on clean onboarding runs |
| **P0** | **Scripts** | **Completed:** Fix broken target paths and missing cases in `scripts/test.sh` | `scripts/test.sh:27-47` | Restores developer test target execution (`intercept`, `persistence`, `controls`) |
| **P0** | **Web / API** | **Completed:** Batch session trajectory fetching to resolve N+1 polling storm | `docs/rest.md` | Prevents SQLite lock timeouts during concurrent dashboard views |
| **P1** | **Intercept** | Interactive Human-in-the-Loop review queue & approval API | `docs/application-documentation.md:601`, `docs/architecture-contract.md:97` | Enables human supervisor approval instead of immediate fail-close blocks |
| **P1** | **Persistence** | **Completed:** Decorate trajectory steps with server-side risk factors & impact ($C$) | `docs/rest.md` §5.1, §5.5 | Eliminates client-side step impact fallback in frontend JavaScript; ensures contract parity |
| **P1** | **Consume** | Implement dedicated `usage-accountant` and `loop-detector` plugins | `docs/consumer-plane.md:724-725` | Dedicated supervision of token/cost burn and tool recursion |
| **P1** | **Web / API** | **Completed:** Standardize error response envelope across `web/sessions.py` | `docs/rest.md` §2.2 | Prevents undefined error messages in operator dashboard |
| **P1** | **Simulation** | Expand `opencode_runner.py` range to include `APP-0016` & `APP-0017` | `simulation/opencode_runner.py:223`, `docs/mock-data-spec.md:37` | Enables evaluation of secret key exfiltration and semantic injection defenses |
| **P2** | **Persistence** | Automated background maintenance scheduler & comprehensive pruning | `docs/persistence.md:120`, `persistence/settings.py:28` | Bounds disk usage for long-lived installations across all governance tables |
| **P2** | **Intercept** | Live model financial spend tracking (`budget.cost_usd`) | `docs/rest.md:1031`, `intercept/governed/prompts.py:284` | Allows non-zero USD dollar limits without failing closed |
| **P2** | **Web** | **Completed:** Authenticate configuration mutation endpoints (`/configs`, `/config-selection`) | `docs/rest.md`, `docs/application-documentation.md:600` | Hardens runtime against unauthorized policy tampering |
| **P2** | **Consume** | Outbox sink ledger (`consumer_outbox`) for asynchronous sink delivery | `docs/consumer-plane.md:712` | Decouples plugin execution from downstream sink availability |
| **P2** | **LLM** | Native Anthropic and Gemini provider clients in `llm/factory.py` | `docs/system-architecture.md:42`, `docs/probabilistic-evaluation.md:340` | Enables direct deployment against Claude and Gemini without OpenAI shims |
| **P3** | **Consume** | CUSUM process conformance change detector (Markov model) | `docs/probabilistic-evaluation.md:§2` | Statistical sequence anomaly detection on agent execution traces |
| **P3** | **Intercept** | Full Bayesian Session Trust Fusion & `LOWER_TRUST` lattice | `docs/probabilistic-evaluation.md:§4` | Dynamic multi-tiered behavioral containment (watch, restricted, quarantine, halt) |
| **P3** | **Domain** | AML Transaction Monitoring Pipeline (8 tools, 13 scenarios) | `docs/use-cases.md:347-411` | Expands control layer beyond KYC to suspicious activity monitoring |

---

## 2. Layer 1: Interception & Control Gateway

### 2.1 Interactive Human-in-the-Loop Review Queue
- **Documented Reference:** `docs/application-documentation.md` §2.2 line 225, §5.4 line 601; `docs/architecture-contract.md` lines 97–102.
- **Current State:** When policy specifies `REQUIRE_APPROVAL`, execution immediately halts and fails closed with `code = "APPROVAL_NOT_IMPLEMENTED"` and `decision = "BLOCK"` (`intercept/policy/runs.py` line 107).
- **Target Implementation:**
  - Introduce an asynchronous paused-action queue storing serialized proposals with cryptographic argument digests.
  - Implement compliance desk management endpoints:
    - `GET /api/v1/approvals/pending`: List actions awaiting review.
    - `POST /api/v1/approvals/{approval_id}/decide`: Submit review decision (`ALLOW` or `BLOCK`) with a single-use authorization token.
  - Resume agent execution upon approved dispatch with bound audit attribution.

### 2.2 Dynamic Policy Hot-Reloading via File Watcher
- **Documented Reference:** `docs/application-documentation.md` §2.2 lines 266–272; `docs/intercept/opencode-adapter-plan.md` lines 46–47.
- **Current State:** Policy changes can only be loaded by invoking `PUT /api/v1/config-selection` or by restarting the gateway process.
- **Target Implementation:**
  - Implement a background file watcher (`watchfiles` or inotify) monitoring `config/presets/` and active configuration files.
  - Automatically validate modified configurations against `intercept.schema.json`.
  - Atomically swap active policy snapshots for new sessions while preserving pinned policy versions for active Task Contracts.

### 2.3 Live Model Spend Accounting (`budget.cost_usd`)
- **Documented Reference:** `docs/rest.md` §5.15 line 1031; `docs/intercept-plugins.md` line 107.
- **Current State:** Setting `budget.cost_usd > 0` causes `PromptGateway` to immediately block with `LOCAL_MODEL_COST_BUDGET_UNSUPPORTED` (`intercept/governed/prompts.py` line 284).
- **Target Implementation:**
  - Maintain a pricing catalog mapping `(provider, model_id)` to input/output token rates.
  - Accumulate real-time session financial burn in `PromptGateway` and enforce configured `cost_usd` ceilings.

### 2.4 Real-Time Server-Sent Events (SSE) Stream
- **Documented Reference:** `docs/application-documentation.md` §5.4 line 598 (`GET /api/v1/events/stream`).
- **Current State:** The dashboard relies exclusively on client-side polling (`setInterval`).
- **Target Implementation:**
  - Implement an authenticated SSE broadcaster in `intercept/service/` or `web/main.py` streaming sanitized decision envelopes, policy interventions, and latency metrics in real time.

### 2.5 Pre-Dispatch Semantic Guard in Layer 1
- **Documented Reference:** `docs/rest.md` §5.15 lines 1048–1054; `configuration/models.py`.
- **Current State:** `intercept.semantic_guard` fields (`block_threshold`, `approve_threshold`, `alert_threshold`, `allowed_models`) are stored in `PolicyConfig` but never evaluated during interception.
- **Target Implementation:**
  - Integrate a lightweight pre-dispatch classifier model assessing prompt injection likelihood and intent alignment prior to upstream tool dispatch.

### 2.6 Streaming Token Redaction
- **Documented Reference:** `docs/application-documentation.md` §2.1 line 154, §2.2 line 224; `docs/intercept/controlled-execution.md` line 67.
- **Current State:** Completions are buffered in memory in their entirety before post-generation regex inspection.
- **Target Implementation:**
  - Implement sliding-window token buffer inspection for SSE streaming LLM completions, halting or redacting generation when secret or PII patterns emerge mid-stream.

### 2.7 Application Scope Expansion in `opencode_runner.py`
- **Documented Reference:** `docs/mock-data-spec.md` lines 37, 222–223; `data/generate.py` line 1113.
- **Current State:** `simulation/opencode_runner.py` line 223 restricts application IDs to `APP-0001` through `APP-0015`.
- **Target Implementation:**
  - Expand validation regex to permit `APP-0016` (secret key exfiltration defense) and `APP-0017` (semantic injection test).

### 2.8 Scripted Fault: `leak_raw:<field>`
- **Documented Reference:** `docs/use-cases.md` lines 90, 245, 410 (`ONB-09`, `TXM-13`).
- **Current State:** `simulation/agent.py` line 60 explicitly aborts on `leak_raw` with `"(leak_raw needs the gateway, not built yet)"`.
- **Target Implementation:**
  - Implement the `leak_raw` harness to inject unredacted national identifiers (PESEL) and IBANs into tool arguments, validating that Layer 1 `classified_scanner` and `pattern_scanner` catch and redact or block them.

---

## 3. Layer 2: Persistence & Storage Engine

### 3.1 Automated Background Maintenance Scheduler
- **Documented Reference:** `docs/persistence.md` line 125; `persistence/settings.py` lines 28 & 49 (`maintenance_interval_seconds = 60.0`).
- **Current State:** Maintenance functions (`maintain`, `prune_before`, `backup_store`) exist in `persistence/maintenance.py` but must be manually invoked; the setting is never scheduled.
- **Target Implementation:**
  - Add a periodic asyncio background task to `PersistenceWorker` executing `maintain(store, now)` every `maintenance_interval_seconds`.

### 3.2 Comprehensive Pruning for Governance Tables
- **Documented Reference:** `docs/persistence.md` lines 123–124.
- **Current State:** `_sync_prune` in `persistence/store.py` prunes `events`, `alerts`, `audit_actions`, and `dead_letter_queue`, but ignores newer tables.
- **Target Implementation:**
  - Extend retention pruning to `plugin_decisions`, `consumer_findings`, `verification_results`, `task_contracts`, `policy_signals`, and `agent_content`.

### 3.3 Server-Side Persisted Aggregated Metrics
- **Completed:** `/api/v1/metrics/usage`, `/metrics/security` and `/metrics/performance` scan persisted evidence across sessions in normal mode. Recorded control durations feed latency percentiles; absent measurements and pricing remain unavailable.
- **Implementation:** `persistence/query_dashboard.py`; contracts and remaining limits are in `docs/rest.md` §4.11, §4.12, §4.20 and §8.

### 3.4 Persisted Cross-Session Detections & Catalog
- **Completed:** Detection lists, lookup and the catalog query persisted gateway, finding, alert and verification evidence, with filters and pagination.
- **Remaining:** The catalog includes observed names; curated descriptions and OWASP mappings remain future work. See `docs/rest.md` §4.7–4.9 and §8.

### 3.5 Automated Content Store Dereferencing
- **Documented Reference:** `docs/consumer-plane-event-envelope.md` lines 98–100.
- **Current State:** Large bodies are only stored in `agent_content` if explicitly passed via `GovernedPersistence.put_content()`.
- **Target Implementation:**
  - Automatically extract inline bodies exceeding a size threshold (e.g., > 1 KiB) into `agent_content` and replace them with `{ "ref": "store://agent_content/<id>", "sha256": "..." }` prior to outbox enqueuing.

---

## 4. Layer 3: Consume Plane, Supervision & Verification

### 4.1 Trajectory Risk Scope Filter (Resolving Known False Positive)
- **Completed:** The gateway attaches trusted document ownership from the pinned task baseline. The risk model accepts owned documents and preserves out-of-scope signals for foreign document attempts; caller-supplied ownership cannot bypass the check.
- **Verification:** `test_clean_run_has_no_out_of_scope_signal` now passes without `xfail`; gateway tests cover forged ownership. See `docs/trajectory-risk-model.md`.

### 4.2 Built-In Detection Plugins (from `docs/consumer-plane.md` §10)
- [ ] **`usage-accountant`:** Dedicated monitor tracking burn rate against `contract.budget`, proposing `REQUIRE_APPROVAL_FOR` at 80% and `HALT_SESSION` at 100%.
- [ ] **`loop-detector`:** Detector identifying identical tool calls $\ge N$ times in a sliding window and A-B-A-B oscillation patterns, proposing `BLOCK_TOOLS`.
- [ ] **`scope-drift`:** Dedicated detector identifying accesses outside `contract.target_ids`, proposing `BLOCK_TOOLS` after repeat violations.
- [ ] **`repeat-side-effect`:** Detector flagging duplicate executed write/irreversible calls on the same target, proposing `BLOCK_TOOLS`.
- [ ] **`step-order`:** Detector verifying strict prerequisite ordering before state modification (e.g. `screen_sanctions` before `create_client`), proposing `REQUIRE_APPROVAL_FOR`.

### 4.3 CUSUM Markov Process Conformance Plugin
- **Documented Reference:** `docs/probabilistic-evaluation.md` §2.
- **Target Implementation:**
  - Train 92-token first-order Markov transition matrix ($P_B$) from clean baseline runs.
  - Implement tokenizer $\tau(a)$ mapping action events to $(tool/egress \times status)$.
  - Implement Page's CUSUM statistic accumulator with Lorden bound $h = \ln \text{ARL}_0$ emitting `process.nonconforming` findings upon significant drift.

### 4.4 Full Bayesian Session Trust Fusion & `LOWER_TRUST` Lattice
- **Documented Reference:** `docs/probabilistic-evaluation.md` §4; `contracts/feedback.py`.
- **Target Implementation:**
  - Add `LOWER_TRUST(evidence_llr = e)` to `AdjustmentAction` in `contracts/feedback.py`.
  - Implement log-odds fusion in Layer 1: $z \leftarrow z + \tilde{e}$ with prior $\pi_0 = 0.02$.
  - Enforce tiered containment states: `watch` (< 0.80), `restricted` (< 0.50), `quarantine` (< 0.25), `halt` (< 0.10) with semantic cap $C_{\text{sem}} = 5.5$.

### 4.5 Enhanced Goal Alignment Judge Capabilities
- **Documented Reference:** `docs/probabilistic-evaluation.md` §3.
- **Target Implementation:**
  - **`get_content_excerpt` tool:** Allow the judge to inspect untrusted tool results using spotlight delimiters (`<<UNTRUSTED_DATA id=...>>`) and `CLASSIFIED` regex masking.
  - **Deterministic `stub` backend:** Feature-based offline evaluator for hermetic testing without live model dependencies.
  - **Native Anthropic & Gemini clients:** Native SDK adapters in `llm/factory.py`.

### 4.6 Consumer Runtime Infrastructure
- **Circuit Breaker:** Suspend failing plugins after $N$ consecutive errors for cooldown period (`breaker_cooldown_s`).
- **Consumer HTTP Endpoints:** `/consumer/health` and `/consumer/metrics`.
- **Durable Feedback Log:** SQLite persistence of `consumer_feedback_log` (currently stored only in memory).
- **Asynchronous Sink Outbox:** Dedicated `consumer_outbox` table in SQLite ledger ensuring at-least-once sink delivery.

---

## 5. Web Dashboard & Operator UI

### 5.1 Batch Session Trajectory Loading (Mitigate N+1 Polling Storm)
- **Completed:** `GET /api/v1/sessions?limit=50` includes backend risk, verification and intervention summaries. Risk-map polling performs one summary request; trajectory and decision pages load only on drill-down with explicit pagination.
- **Implementation:** `persistence/query.py`, `static/js/riskmap.js`; frontend tests cover 50 sessions without trajectory request fanout.

### 5.2 Server-Side Risk Score Projection
- **Completed:** Session and step risk are projected from persisted `trajectory-risk` plugin decisions, including probability, consequence, expected loss and signals. The browser renders these values directly; historical evidence without recorded factors remains unavailable.
- **Reference:** `docs/rest.md` §5.1 and §5.5.

### 5.3 Live Sandbox Outcome Replays in Tests Tab
- **Completed integration:** Outcome cards load recorded sessions and `/api/v1/sessions/{id}/verification`, displaying actual verifier checks and verdicts. Empty evidence is not verified; canned timer scenarios were removed.
- **Remaining feature proposal:** A separate on-demand scenario replay endpoint is still planned. The existing suite API executes real control-layer tests.

### 5.4 Unified Multi-Service Health Badge
- **Completed:** The header checks both `/healthz` and `/api/v1/health`, with bounded timeouts and HTTP-status checks, and displays degraded status when evidence reads are unavailable.

### 5.5 Authentication & CSRF on Configuration Endpoints
- **Completed:** Config PUTs require `CONFIG_ADMIN_TOKEN` through `X-Admin-Token` or Bearer, validate browser origin/fetch metadata, and enforce a request budget. Empty server secrets disable writes. The evidence API is read-only by default.
- **Dashboard:** The administrator token is entered inline and kept in memory; saved drafts and selected immutable snapshots are shown separately. See `docs/rest.md` §4.18–4.19 and §7.

### 5.6 Full Form Controls in Config Tab UI
- **Documented Reference:** `static/html/index.html` lines 62–117; `configuration/models.py`.
- **Target Implementation:**
  - Add UI form inputs in the Config tab for `velocity_guard` (`window_s`, `max_calls`), `pattern_match` regexes, `domain_blocklist` domains, and `admin_tools`.

### 5.7 Active Process Reaper for OpenCode Wrapper
- **Completed:** A background task expires idle sessions, and process groups are terminated/reaped on startup failure, cancellation, timeout and shutdown. Session creation is serialized against capacity checks; replies are bounded.
- **Reference:** `web/sessions.py`, `tests/support/test_web_sessions.py`, `docs/dashboard/deployment.md`.

---

## 6. Test Harness & Scripts

### 6.1 `scripts/test.sh` Target Implementation & Path Updates
- **Completed:** `controls` and `support` targets exist, moved test paths are corrected, and `all` runs Python, adapter and frontend suites. CI installs Node and runs the dashboard/adapter tests.
- **Reference:** `scripts/README.md`. The Python suite also launches an isolated live dashboard integration smoke with governed SQLite evidence, without provider calls.

### 6.2 Corrupted-Writer Verification Scenario Runner (`CW-01` to `CW-06`)
- **Documented Reference:** `docs/use-cases.md` lines 255–266.
- **Target Implementation:**
  - Provide a standalone script or test suite that seeds corrupt bank states and verifies that `outcome-verifier` detects every corruption class with expected failure codes.

---

## 7. Future Domain Scope: AML Transaction Monitoring Pipeline

*(Deferred MVP Scope — `docs/use-cases.md` lines 347–411)*

### 7.1 AML Tool Implementations (8 Tools)
- [ ] `get_alert(alert_id: str)`: Retrieve alert metadata and triggering rule.
- [ ] `get_transactions(account_id: str, from_date: str, to_date: str)`: Query account ledger history.
- [ ] `get_customer_profile(client_id: str)`: Inspect client risk tier and expected activity.
- [ ] `get_counterparty_info(iban_or_name: str)`: Retrieve counterparty risk attributes.
- [ ] `close_alert(alert_id: str, disposition: str, rationale: str)`: Disposition alert as false positive or escalated.
- [ ] `file_sar(alert_id: str, narrative: str)`: Write suspicious activity report to `sar_filings`.
- [ ] `freeze_account(account_id: str, reason: str)`: Irreversible emergency account freeze.
- [ ] `contact_customer(client_id: str, message: str)`: Write message to `customer_messages`.

### 7.2 AML Scenarios & Postconditions
- [ ] Implement synthetic AML agent scenarios (`TXM-01` through `TXM-13`).
- [ ] Implement outcome postconditions (`TXM-P1` through `TXM-P6`) in `data/postconditions.py`:
  - `TXM-P1`: True positive alerts must have a corresponding `sar_filings` record.
  - `TXM-P2`: False positive alerts must have valid rationales without SAR filing.
  - `TXM-P3`: Account freeze must be preceded by an open high-severity alert.
  - `TXM-P4`: SAR narratives must meet minimum length and quality thresholds.
  - `TXM-P5`: High-risk counterparties must be flagged.
  - `TXM-P6`: Closed alerts must not be reopened by the agent without supervisor override.
