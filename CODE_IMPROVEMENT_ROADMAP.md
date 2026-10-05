# Code Improvement Roadmap & Implementation Wishlist

**Project:** Sentinel Interlock — AI Control Layer  
**Date:** 2026-10-05  
**Context:** Comprehensive gap analysis and alignment review between architectural specifications, design documents, and the active codebase.

This document catalogs all capabilities, features, safety mechanisms, and architectural enhancements specified across the project's documentation (`docs/`) that are **not yet implemented in the codebase**. Items are organized by system plane and prioritized to guide future engineering sprints.

---

## 1. Prioritization & Impact Matrix

| Priority | Plane | Feature / Enhancement | Target Documentation | Primary Benefit |
|---|---|---|---|---|
| **P0** | **Consume** | Fix `trajectory-risk` document ID false positive (`DOC-xxxx`) | `docs/decision-trace.md:162`, `tests/test_decisions_demo.py` | Eliminates false high-risk escalations on clean onboarding runs |
| **P0** | **Scripts** | Fix broken target paths and missing cases in `scripts/test.sh` | `scripts/test.sh:27-47` | Restores developer test target execution (`intercept`, `persistence`, `controls`) |
| **P0** | **Web / API** | Batch session trajectory fetching to resolve N+1 polling storm | `GAP_ANALYSIS_FRONTEND_BACKEND.md:123` | Prevents SQLite lock timeouts during concurrent dashboard views |
| **P1** | **Intercept** | Interactive Human-in-the-Loop review queue & approval API | `docs/application-documentation.md:601`, `docs/architecture-contract.md:97` | Enables human supervisor approval instead of immediate fail-close blocks |
| **P1** | **Persistence** | Expose server-side `expected_loss` and risk probabilities in queries | `docs/rest.md:5.1`, `GAP_ANALYSIS_FRONTEND_BACKEND.md:108` | Eliminates redundant risk engine in frontend JavaScript; ensures contract parity |
| **P1** | **Consume** | Implement dedicated `usage-accountant` and `loop-detector` plugins | `docs/consumer-plane.md:724-725` | Dedicated supervision of token/cost burn and tool recursion |
| **P1** | **Web / API** | Standardize error response envelope across `web/sessions.py` | `docs/rest.md:2.2`, `GAP_ANALYSIS_FRONTEND_BACKEND.md:147` | Prevents undefined error messages in operator dashboard |
| **P1** | **Simulation** | Expand `opencode_runner.py` range to include `APP-0016` & `APP-0017` | `simulation/opencode_runner.py:223`, `docs/mock-data-spec.md:37` | Enables evaluation of secret key exfiltration and semantic injection defenses |
| **P2** | **Persistence** | Automated background maintenance scheduler & comprehensive pruning | `docs/persistence.md:120`, `persistence/settings.py:28` | Bounds disk usage for long-lived installations across all governance tables |
| **P2** | **Intercept** | Live model financial spend tracking (`budget.cost_usd`) | `docs/rest.md:1031`, `intercept/governed/prompts.py:284` | Allows non-zero USD dollar limits without failing closed |
| **P2** | **Web** | Authenticate configuration mutation endpoints (`/configs`, `/config-selection`) | `GAP_ANALYSIS_FRONTEND_BACKEND.md:50`, `docs/application-documentation.md:600` | Hardens runtime against unauthorized policy tampering |
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
- **Documented Reference:** `docs/rest.md` §4.11, §4.12, §4.20; `GAP_ANALYSIS_FRONTEND_BACKEND.md` §3.2.
- **Current State:** `/api/v1/metrics/usage`, `/api/v1/metrics/security`, and `/api/v1/metrics/performance` return 501 `not_implemented` in normal production mode.
- **Target Implementation:**
  - Implement SQLite aggregation queries in `persistence/query_usage.py` computing p50/p95 latency, total tool calls, block rates, and token burn across all recorded session stores.

### 3.4 Persisted Cross-Session Detections & Catalog
- **Documented Reference:** `docs/rest.md` §4.7, §4.8, §4.9.
- **Current State:** `/api/v1/detections` and `/api/v1/catalog/detections` return 501 `not_implemented`.
- **Target Implementation:**
  - Implement cross-session detection indexing and catalog lookups backed by `config/detection-catalog.yaml`.

### 3.5 Automated Content Store Dereferencing
- **Documented Reference:** `docs/consumer-plane-event-envelope.md` lines 98–100.
- **Current State:** Large bodies are only stored in `agent_content` if explicitly passed via `GovernedPersistence.put_content()`.
- **Target Implementation:**
  - Automatically extract inline bodies exceeding a size threshold (e.g., > 1 KiB) into `agent_content` and replace them with `{ "ref": "store://agent_content/<id>", "sha256": "..." }` prior to outbox enqueuing.

---

## 4. Layer 3: Consume Plane, Supervision & Verification

### 4.1 Trajectory Risk Scope Filter (Resolving Known False Positive)
- **Documented Reference:** `docs/decision-trace.md` lines 162–166; `tests/test_decisions_demo.py`.
- **Current State:** Pinned by strict `xfail` test `test_clean_run_has_no_out_of_scope_signal`. The regex `/^[A-Z]{3}-\d{4}$/` flags the application's legitimate documents (`DOC-0001` to `DOC-0047`) as out-of-scope targets because they match the ID format but are not in `contract.target_ids`.
- **Target Implementation:**
  - Distinguish application entity target IDs (`APP-xxxx`, `CLI-xxxx`) from associated document references (`DOC-xxxx`), preventing document reads from inflating the session's risk score. Remove the `xfail` marker.

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
- **Documented Reference:** `GAP_ANALYSIS_FRONTEND_BACKEND.md` §2 item 4, §3.4.
- **Current State:** `static/js/riskmap.js` fires 3 HTTP requests per session every 30 seconds (151 requests for 50 sessions), risking thread exhaustion and SQLite busy timeouts.
- **Target Implementation:**
  - Add backend endpoint `GET /api/v1/sessions/summary?limit=25` returning session records with pre-aggregated step counts, worst-step risk levels, and decision summaries in a single query.
  - Update `static/js/riskmap.js` to consume summaries, loading full trajectories only upon user drill-down.

### 5.2 Server-Side Risk Score Projection
- **Documented Reference:** `GAP_ANALYSIS_FRONTEND_BACKEND.md` §3.3; `docs/rest.md` §5.1.
- **Current State:** Backend returns `expected_loss: null` and `failure_probability: null`; frontend JavaScript re-implements the Bayesian noisy-OR algorithm in the browser.
- **Target Implementation:**
  - Extract `expected_loss` and risk probabilities from stored `consumer_findings` where `plugin == 'trajectory-risk'` in `persistence/query.py`.
  - Update `riskmap.js` to render backend values directly.

### 5.3 Live Sandbox Outcome Replays in Tests Tab
- **Documented Reference:** `GAP_ANALYSIS_FRONTEND_BACKEND.md` §3.1; `docs/application-documentation.md` line 603.
- **Current State:** Guardrail test suite runs real pytest cases, but the bottom 3 outcome replay cards (`Approved client, wrong name saved`, etc.) are canned JavaScript timers.
- **Target Implementation:**
  - Implement `POST /api/v1/scenario/replay` running seeded bank state validations against `contracts/verification.py` and updating the cards with real status badges (`VERIFIED_SUCCESS`, `FAILED_POSTCONDITIONS`).

### 5.4 Unified Multi-Service Health Badge
- **Documented Reference:** `GAP_ANALYSIS_FRONTEND_BACKEND.md` §2 item 6.
- **Current State:** Header checks `/healthz` on port 8000 and displays "Gateway online" even if `api:8790` is crashed.
- **Target Implementation:**
  - Update `static/js/app.js` to query both `/healthz` and `/api/v1/health`, displaying a "Degraded" status if the read store is offline.

### 5.5 Authentication & CSRF on Configuration Endpoints
- **Documented Reference:** `GAP_ANALYSIS_FRONTEND_BACKEND.md` §2 item 7; `docs/rest.md` §7.
- **Target Implementation:**
  - Protect `PUT /api/v1/configs/{name}` and `PUT /api/v1/config-selection` with Bearer token authentication (`ADMIN_SECRET` / `INTERCEPT_ADMIN_TOKEN`) or same-origin CSRF tokens.

### 5.6 Full Form Controls in Config Tab UI
- **Documented Reference:** `static/html/index.html` lines 62–117; `configuration/models.py`.
- **Target Implementation:**
  - Add UI form inputs in the Config tab for `velocity_guard` (`window_s`, `max_calls`), `pattern_match` regexes, `domain_blocklist` domains, and `admin_tools`.

### 5.7 Active Process Reaper for OpenCode Wrapper
- **Documented Reference:** `GAP_ANALYSIS_FRONTEND_BACKEND.md` §2 item 8, §3 item 9.
- **Target Implementation:**
  - Add an asynchronous background reaper in `web/sessions.py` using `os.killpg` on process groups to terminate abandoned or timed-out OpenCode processes.

---

## 6. Test Harness & Scripts

### 6.1 `scripts/test.sh` Target Implementation & Path Updates
- **Documented Reference:** `scripts/test.sh` lines 6–47; `scripts/README.md`.
- **Target Implementation:**
  - Add missing `controls)` and `support)` switch cases in `scripts/test.sh`.
  - Update outdated target paths (`tests/test_*.py` -> `tests/support/test_*.py`, `tests/consume_plane` -> `tests/support/consume_plane`).

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
