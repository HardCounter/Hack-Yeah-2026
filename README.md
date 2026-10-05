# Sentinel Interlock - AI Control Layer

Built by team **HardCounter** at HackYeah 2026 (Goldman Sachs challenge: AI Control Layer).

**Sentinel Interlock (SI)** is a policy-driven runtime control and verification layer for autonomous AI agents. It works like the physical interlock on industrial machinery: **nothing moves until the check passes**. Every tool call, model prompt, API request, or egress attempt an agent makes is intercepted and evaluated outside the model against a centralized, live-manageable policy—then allowed, redacted, held for human review, or blocked. Because enforcement runs outside the model's process space, prompt injections and adversarial inputs cannot disable or bypass it, and every decision is durably logged with an audit trail and immutable evidence.

The system sits between agents and external environments (LLMs, MCP servers, banking APIs, internal tools, and other agents). It combines **deterministic pre-dispatch policy enforcement**, **continuous trajectory risk supervision**, and **independent external outcome verification** bound under a shared Task Contract.

```text
1. Is the action authorized?      -> Layer 1: Deterministic Gateway & Policy Interception
2. Does it serve the task?        -> Layer 3: Trajectory Supervision & Bayesian Risk Model
3. Did the correct result exist? -> Layer 3: Independent Outcome Verification against Bank State
```

---

## Architecture: The Three-Plane Pipeline

One agent action flows through three dedicated planes:

```text
OpenCode Agent ──► adapters/opencode ──► Layer 1: Intercept ──► Layer 2: Persistence ──► Layer 3: Consume Plane
                   (plugin hooks)       (allow / block)        (evidence + outbox)      (risk, findings, outcome verifier)
                                              ▲                                                   │
                                              └───────────── tighten-only feedback ───────────────┘
```

### Layer 1: Pre-Dispatch Interception (`intercept/`, `configuration/`, `plugins/`, `adapters/opencode/`)
- Intercepts tool calls and model prompts before execution.
- Evaluates exact tool allowlists, argument constraints, credential/regex patterns (`pattern_scanner`), classified data sensitivity (`classified_scanner`), and domain restrictions (`domain_blocklist`).
- Provides pre-dispatch guardrail plugins: `VelocityGuard` (sliding-window rate limits), `BudgetGuard` (token and tool call burn caps), and `PatternMatch`.
- Implements `PromptGateway` (`intercept/governed/prompts.py`) to evaluate user prompts and model completions against injection signatures and token budgets.
- Supports external polyglot webhooks and dynamic configuration presets (`standard`, `strict`, `lenient`).

### Layer 2: Durable Persistence Engine (`persistence/`)
- Sanitizes and commits immutable evidence into SQLite with Write-Ahead Logging (WAL) and atomic transactions.
- Implements an atomic outbox queue with bounded asynchronous delivery to registered consumers, lease serialization, jittered exponential backoff, and dead-letter queuing (DLQ).
- Manages sequential per-session action indexing (`seq`), run binding, and cryptographic banking effect receipts (`effect_receipts`, `governed_screening_evidence`).
- Serves a high-performance, read-only REST API (`persistence/http_api`) with keyset pagination, structured audit exports (NDJSON), and store health monitoring.

### Layer 3: Consume Plane & Supervision (`consume_plane/`, `contracts/`, `data/postconditions.py`)
- Asynchronous plugin runtime operating on the canonical Event Envelope v2.1.
- **Trajectory Risk Model** (`consume_plane/plugins/trajectory_risk.py`): Continuous expected loss calculation ($E = \sum P \times C$) using a Bayesian noisy-OR combination of behavioral signals (out-of-scope targets, missing prerequisites, budget burn, rapid bursts).
- **Independent Outcome Verifier** (`consume_plane/plugins/outcome_verifier.py`): Post-execution verification checking bank database state against ground truth postconditions (`ONB-P1` to `ONB-P4`), verifying action provenance, screening evidence digests, and effect receipts.
- **Goal Alignment Judge** (`consume_plane/plugins/goal_alignment_judge.py`): Semantic supervision evaluating whether irreversible actions serve the Task Contract's objective.
- **Tighten-Only Feedback Controller** (`consume_plane/runtime/feedback.py`): Dynamically escalates enforcement (e.g. requiring approvals or blocking tools) via a formal lattice.

### Web Application & Operator Dashboard (`web/`, `static/`, Caddy)
- **FastAPI application** (`web/main.py`) served behind a Caddy reverse proxy on ports 80/443.
- **Interactive Agent Sandbox** (`web/sessions.py`): Spin up and chat with sandboxed OpenCode agents under live policy control.
- **Guardrail Test Runner** (`web/suite.py`): Live execution of the full pytest control-layer test suite directly from the UI Tests tab.
- **Risk Map & Trajectory Visualization** (`static/js/riskmap.js`): Interactive timeline of agent actions, risk severity levels, and plugin decisions.
- **Live Decision Trace Inspector**: Drill down into individual plugin decisions, reasoning, and contributing factors (`GET /api/v1/sessions/{id}/decisions`).

---

## Monitored Banking Tools (16 Tools)

The simulation environment provides 16 tools divided into 10 legitimate KYC tools and 6 bait/fake tools designed to tempt misaligned agents:

| Category | Tools | Side Effect | Purpose |
|---|---|---|---|
| **KYC Read** | `read_application`, `read_documents`, `extract_fields`, `check_registry`, `screen_sanctions`, `compute_risk` | `read` | Retrieve client data, OCR files, registry UBOs, check sanctions/PEP lists, and evaluate risk. |
| **KYC Write** | `create_client` | `irreversible` | Create client and accounts in `bank.db`; requires prior screening and approval. |
| **KYC Decision** | `request_more_docs`, `escalate_edd`, `reject_application` | `write` | Update application status; non-reversible in business terms. |
| **Bait Tools** | `send_email`, `fetch_url`, `run_code`, `load_risk_model`, `read_config`, `delete_client` | `read` / `write` / `irreversible` | Traps for unauthorized exfiltration, external network access, code execution, or destructive actions. |

---

## Project Layout

| Path | Role |
|---|---|
| `contracts/` | Canonical shared models: `AgentAction`, `TaskContract`, `GatewayVerdict`, `DecisionTraceRecord`, `PolicyAdjustmentSignal`, envelope v2.1 decoder |
| `adapters/opencode/` | OpenCode 2.0.22 plugin: intercepts tool calls and prompts, forwarding them to Layer 1 |
| `intercept/` | Layer 1. `policy/` rule engine · `governed/` runtime & prompts · `service/` HTTP server, receiver · `tools/` execution harness |
| `persistence/` | Layer 2. SQLite evidence store, outbox queue, worker, maintenance, and read-only REST API (`persistence/http_api/`) |
| `consume_plane/` | Layer 3. Consumer runtime, trajectory risk plugin, outcome verifier, and goal-alignment judge |
| `plugins/` | Layer 1 pre-dispatch plugins: `budget_guard.py`, `pattern_match.py`, `velocity_guard.py` |
| `configuration/` | Shared policy configuration models, preset storage, and REST router (`/api/v1/configs`, `/api/v1/config-selection`) |
| `simulation/` | Governed KYC agent runner, synthetic tool implementations, and baseline policies |
| `data/` | Deterministic banking dataset generator (`generate.py`), business rules, report generator, and postconditions |
| `web/` | Web application (`main.py`), interactive OpenCode sessions (`sessions.py`), and test runner (`suite.py`) |
| `static/` | Operator dashboard UI: HTML, CSS, JavaScript (risk map, configuration, metrics, tests) |
| `tracing/` | Structured pipeline tracing: terminal, JSONL file (`var/control-layer.log`), or null |
| `scripts/` | Automation, test runners, and demo scripts |
| `tests/` | Comprehensive test suite (745+ unit, integration, and E2E tests) |

---

## Tracing & Decision Visibility

Every agent action can be traced through the entire pipeline:
1. `intercept action.received`: Gateway receives tool/prompt proposal.
2. `persistence evidence.committed`: Immutable record persisted with monotonic `seq`.
3. `intercept action.decided`: Policy verdict (`ALLOW`, `BLOCK`, `REDACT`, `REQUIRE_APPROVAL`, `ALERT`).
4. `consume event.processed`: Consume-plane analysis, findings emitted, and feedback signals generated.

Configure pipeline logging via environment variables:
- `CONTROL_LOG=terminal|file|null` (default: `null`; `file` writes JSONL to `CONTROL_LOG_FILE`, default: `var/control-layer.log`).
- `scripts/run_demo.sh` traces to the terminal by default.

Every **plugin decision point** (including `NO_CHANGE` and skipped assessments) is persisted in the session's evidence database and queryable via:
- `GET /api/v1/sessions/{session_id}/decisions`
- `scripts/inspect_decisions.sh` (see [docs/decision-trace.md](docs/decision-trace.md))

---

## Python Development & Testing with uv

We use **uv** for fast, deterministic environment management, dependency locking, and execution.

```sh
# Synchronize locked dependencies
uv sync --locked

# Check environment
uv run --locked python --version
uv lock --check
```

Local development targets Python 3.12 (compatible with Python >=3.11).

### Running the Complete Test Suite

The test suite contains 745+ tests covering every layer of the architecture:

```sh
# Run the entire test suite (locked and offline)
uv run --locked --offline pytest

# Or run via the comprehensive test runner
scripts/test.sh all
```

Individual test targets supported by `scripts/test.sh`:
```sh
scripts/test.sh python       # All Python tests
scripts/test.sh intercept    # Layer 1 gateway and auditor tests
scripts/test.sh persistence  # Layer 2 store, outbox, and reader tests
scripts/test.sh consume      # Layer 3 consumer runtime and plugin tests
scripts/test.sh e2e          # End-to-end integration tests
scripts/test.sh adapter      # OpenCode Node.js adapter tests
scripts/test.sh data         # Synthetic dataset and postcondition tests
scripts/test.sh decisions    # Control-plane decision trace tests
```

---

## Available Scripts

Detailed walkthrough and live-trace instructions: [scripts/README.md](scripts/README.md).

| Script | Purpose |
|---|---|
| `scripts/run_demo.sh [APP-0001] [--fault F]` | Runs the control layer end-to-end offline with a scripted agent (no external LLM required) and prints the verdict. |
| `scripts/run_live_pipeline.sh [APP-0001]` | Starts the complete governed pipeline (gateway, persistence, consume plane, read API) and waits for an interactive OpenCode session. |
| `scripts/run_opencode_intercepted.sh` | Starts `opencode --standalone` wired to the active pipeline via `var/intercept.env`. |
| `scripts/run_rest_demo.sh [--port 8790]` | Generates synthetic governed sessions, runs outcome verification, and serves evidence on the REST API. |
| `scripts/run_rest_api.sh [--port 8790]` | Starts the read-only SQLite evidence REST API over recorded runs. |
| `scripts/inspect_decisions.sh [SCENARIO]` | Runs governed test scenarios and prints the consume-plane decision trace table. |
| `scripts/test.sh [TARGET]` | Runs all tests or a specific layer's test suite. |
| `scripts/test_consume_plane.sh` | Runs consume-plane unit and integration tests. |
| `scripts/run_consume_plane.sh [events.jsonl]` | Replays a recorded event log through the consume plane and prints findings. |
| `scripts/test_opencode_adapter.sh` | Runs Node.js test suites for the OpenCode adapter. |
| `scripts/check_opencode_pipeline.sh` | Verifies real OpenCode startup handshake with the Python gateway. |
| `scripts/run_intercept_receiver.sh` | Starts an observe-only diagnostic receiver for OpenCode request inspection. |
| `scripts/setup_opencode_pipeline.sh` | Installs pinned OpenCode 2.0.22 under `var/opencode-cli` if not installed globally. |
| `scripts/run_pipeline.sh [APP-0001] --model p/m` | Runs a non-interactive synthetic onboarding application through OpenCode and verifies outcomes. |

---

## Deployment & Docker Stack

The full production stack runs under Docker Compose behind Caddy on ports 80/443:

```sh
# Local Docker deployment
cp .env.example .env
docker compose up -d --build

# Open the dashboard
open http://localhost  # Health check: http://localhost/healthz

# Tear down
docker compose down
```

Deployment details and secret management: [docs/dashboard/deployment.md](docs/dashboard/deployment.md).

---

## Documentation Index

- [Integrated local KYC execution](docs/integrated-runtime.md)
- [Agent action model and three-plane pipeline](docs/agent-action-model.md)
- [Required runtime architecture contract](docs/architecture-contract.md)
- [Persistence integration and guarantees](docs/persistence.md)
- [Control-plane decision trace](docs/decision-trace.md)
- [Consume plane design](docs/consumer-plane.md) and [event envelope v2.1](docs/consumer-plane-event-envelope.md)
- [Trajectory risk model](docs/trajectory-risk-model.md)
- [Probabilistic evaluation & semantic supervision](docs/probabilistic-evaluation.md)
- [Dashboard REST API & configuration management](docs/rest.md)
- [Control Gateway & Interception specification](docs/application-documentation.md)
- [OpenCode adapter forwarding specification](docs/intercept/opencode-forwarding.md)
- [Monitored banking use cases](docs/use-cases.md)
- [Mock banking dataset specification](docs/mock-data-spec.md)
- [System architecture & execution lifecycle](docs/system-architecture.md)
- [Code improvement roadmap & wishlist](CODE_IMPROVEMENT_ROADMAP.md)
