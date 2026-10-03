# Monitored Banking Agents: System Architecture

Status: **proposed, not implemented** apart from the synthetic data generator. The MVP
monitors one **Client Onboarding (KYC)** workflow, intercepted by a synchronous
**AI Control Layer** and an independent persisted-state verifier. **AML is deferred**,
even though the dataset includes AML fixtures. No runtime controls or product tests exist yet.

The normative execution/trust requirements are in [architecture-contract.md](architecture-contract.md).
That contract resolves older proposals below; stack options are suggestions, not selections.

---

## 1. Design principles

1. **Enforcement is external to agents.** Agents use the gateway; backend credentials, tool implementations and writable state are isolated from them. A base URL alone does not prevent bypass.
2. **The proxy is the single control and observation point.** Every LLM call and every tool call crosses it synchronously. It enforces policy (ALLOW, BLOCK, REDACT, REQUIRE_APPROVAL, ALERT) before execution.
3. **The monitoring layer is a separate deployable.** It consumes the event stream and shares no code or imports with the monitored agents. The only contract is the event schema (section 6).
4. **Ground truth must be protected.** The synthetic oracle is generated as `data/ground_truth.json`. Agents receive scoped tool results, not filesystem/SQL access to either data file. Verifier access must be separately read-only.
5. **Deterministic fixtures are replayable.** Scripted scenarios can be reproducible; live agent and semantic-model results are not deterministic. Replay verification also requires an immutable persisted-state snapshot.

---

## 2. System overview

```mermaid
flowchart LR
    subgraph SIM["Monitored Banking Environment"]
        GEN[Data generator<br/>bank.db (SQLite)]
        ORCH[Scenario Runner /<br/>Orchestrator]
        subgraph AGENTS["Monitored Agents"]
            ONB[Client Onboarding<br/>Agent]
            AML[AML Agent<br/>DEFERRED]
        end
        TOOLS[Tool servers<br/>bank.db SQLite tools<br/>OCR, Sanctions, Registry]
        HQ[Compliance / Human<br/>Review Queue]
    end

    subgraph PROXY["AI Control Layer (Proxy)"]
        LLMP[LLM proxy<br/>(Ollama / vLLM / OpenAI)]
        TOOLP[Tool proxy<br/>(In-line guardrails)]
        POL[Pinned Policy + Trusted Task Contract<br/>Hard Checks + Selective Semantic Gate]
    end

    subgraph MON["Evaluation and metrics layer (separate)"]
        BUS[(Event log / queue)]
        STORE[(Analytics store<br/>DuckDB / SQLite)]
        EVAL[Evaluator /<br/>Outcome Verifier]
        DASH[Dashboard / reports<br/>(Streamlit)]
    end

    API[(Local / Upstream LLM)]
    GT[(Sealed ground truth<br/>ground_truth.json)]

    GEN --> TOOLS
    GEN -. writes after setup .-> GT
    ORCH --> AGENTS
    AGENTS --> LLMP --> API
    AGENTS --> TOOLP --> TOOLS
    LLMP --- POL
    TOOLP --- POL
    LLMP --> BUS
    TOOLP --> BUS
    ORCH --> BUS
    HQ --> BUS
    BUS --> STORE --> DASH
    STORE --> EVAL
    TOOLS -. separate read-only persisted-state path .-> EVAL
    EVAL --> DASH
    GT -. read-only postconditions .-> EVAL
```

---

## 3. Banking Simulation Environment

Runs independently of downstream monitoring. Powered by the deterministic mock dataset defined in `docs/mock-data-spec.md`.

### 3.1 Data generator (`data/bank.db` & `data/ground_truth.json`)

| Item | Detail |
|---|---|
| Generator | Implemented stdlib-only `random.Random`, seed 2026; generator contains a byte-identical-output self-check. No Faker dependency. |
| Volumes | Targets: ~150 clients, 230 accounts, 9,000 transactions (90 days history), 15 applications, 60 alerts; actual counts must come from execution. |
| Tables | `clients`, `accounts`, `transactions`, `onboarding_applications`, `alerts`, `company_registry`, `sanctions_list`, `pep_list`, `documents`; runtime evidence/linkage additions are still required. |
| Planted Anomalies | Sanctions hits, PEP flags, prompt injections in OCR/memos, expired passports, structuring cash deposits, rapid movement via high-risk jurisdictions. |
| Sealed Ground Truth | Written to `data/ground_truth.json` (contains actual labels, expected dispositions, and correct resolutions; unseen by agents). |

### 3.2 Monitored Agents (defined in `docs/use-cases.md`)

| Agent | Objective | Key Tools | Authority & Guardrails |
|---|---|---|---|
| **Client Onboarding Agent** | Triage and decide onboarding applications (`APP-…`) | `read_application`, `read_documents`, `extract_fields`, `check_registry`, `screen_sanctions`, `compute_risk`, `create_client`, `request_more_docs`, `escalate_edd`, `reject_application` | `create_client` writes to `clients` & `accounts`; requires prior sanctions screening and unexpired docs. |
| **AML Transaction Monitoring Agent (deferred)** | Future alert investigation (`ALR-…`) | Deferred tools in `docs/use-cases.md` | Not part of the KYC MVP or claimed coverage. |

A planned agent runs against a configured model through the proxy. Model/provider choice is
not yet implemented. Correlation headers (section 4.3) carry no authorization authority.

### 3.3 Tool implementation

Planned Python tools execute over an isolated copy of `data/bank.db`. KYC writes must
atomically persist client/account linkage, application decision and durable effect receipts,
with uniqueness by application across sessions. The verifier uses a separate read-only path.
Neither tools nor these schema additions are implemented yet.

### 3.4 Orchestrator / Scenario Runner

Creates trusted immutable Task Contracts, isolates state per run and drives the planned KYC,
bait and gateway scenarios in `docs/use-cases.md`. Fault injection is test-only and occurs
before enforcement; an agent cannot enable it. AML scenarios are deferred, and scenario
tables are not executed test results.

### 3.5 Human Review Queue (Compliance Desk)

Receives escalations (`escalate_edd`, approval requests) and records human decisions. Allows measuring escalation precision and human-in-the-loop latency.

---

## 4. Proxy layer

The proxy sits between agents and everything they call. It is the only source of per-call telemetry.

### 4.1 LLM proxy

- Exposes an API-compatible endpoint (same request/response shape as the upstream provider), so agents need no code changes beyond `base_url`.
- Forwards only after hard checks, atomic budget reservation and any required semantic gate.
- Buffers responses and inspects outputs before delivery; unchecked streaming is deferred.
- Records sanitized allowlisted metadata: model/version, usage, latency, status, retries and decisions. No raw prompt/completion or secret-bearing blob retention.
- Reserves cost/token/compute bounds synchronously from centralized policy, including semantic calls and retries; reconciles actual or conservatively unknown usage.

### 4.2 Tool proxy

- Fronts all tool servers. Agents call tools through it.
- Records sanitized tool/action metadata, decision, receipt reference, latency and bytes; raw arguments/results are not general audit evidence.
- Can enforce permission scopes per agent identity (e.g. Onboarding Agent may not call `freeze_account` or `file_sar`, and AML Agent may not call `contact_customer` after filing a SAR). A denied call is itself an event.

### 4.3 Correlation headers

Every agent request includes:

| Header | Meaning |
|---|---|
| `X-Run-Id` | One simulation run |
| `X-Case-Id` | One case being worked (`APP-0001`, `ALR-0002`, ...) |
| `X-Agent-Id` | Which agent (`onboarding-agent`, `admin-agent`; `aml-agent` deferred) |
| `X-Step-Id` | Monotonic step counter within the case |
| `X-Parent-Span-Id` | Links a call to the step that triggered it |

These help rebuild traces without agent internals; they are **not credentials**. The gateway
binds the authenticated principal to the stored contract, rejects conflicting IDs and
allocates authoritative action IDs/order. Agents cannot supply their own role or authority.

### 4.4 Synchronous Policy Enforcement & Scripted Faults

The proxy evaluates deterministic checks before requests reach upstream LLMs or tools,
then selectively assesses the trajectory before high-impact dispatch. Hard denies, exhausted
budgets and missing approvals cannot be overridden. Semantic timeout/invalid response pauses
or blocks when that gate is required. Exact-action approvals are single-use and state-bound:
- **Synchronous Policy Actions:** `ALLOW`, `BLOCK`, `REDACT` (PII/secrets), `REQUIRE_APPROVAL`, `ALERT`.
- **Scripted Faults (for reproducible testing as defined in `docs/use-cases.md`):**
  - `skip_step:<tool>` (e.g. skip sanctions check before creating client).
  - `swap_arg:<tool>.<arg>=<value>` (e.g. swap recipient name or frozen account ID).
  - `repeat:<tool>` (e.g. duplicate client creation or repeat SAR filing).
  - `extra_call:<tool>(<args>)` (e.g. browsing unrelated customers or tipping off customer after SAR).
  - `loop:<tool>:<n>` (e.g. infinite document reading to test budget exhaustion).
  - `leak_raw:<field>` (e.g. leaking raw PESEL or IBAN).

Injected faults are tagged in the event (`fault_injected: true`) so the Evaluator can distinguish planted attack attempts from organic model errors.

### 4.5 Emission

The gateway persists sanitized critical decisions/intents before high-impact dispatch;
business writes atomically record effect receipts. A durable outbox asynchronously delivers
analytics events. Queues/retries are bounded and event-ID deduplicated. Durable audit failure
pauses writes; an in-memory buffer cannot guarantee evidence preservation after a crash.

---

## 5. Monitoring and metrics layer

A separate verifier/metrics component. Metrics use sanitized events. Outcome verification
uses the trusted contract/baseline and a separate read-only consistent snapshot of persisted
bank state, with the synthetic oracle where relevant. It never relies on agent claims or
tool responses; event/ground-truth joins alone cannot prove a business outcome.

### 5.1 Components

| Component | Responsibility |
|---|---|
| **Event log / bus** | Append-only record of all events. Start with SQLite / local JSONL; upgrade to Kafka/Redpanda if live streaming cluster is required |
| **Ingestor** | Validates events against the schema, writes to the analytics store |
| **Analytics store** | DuckDB or SQLite (Postgres / ClickHouse for large scale). Tables: `llm_calls`, `tool_calls`, `lifecycle`, `faults`, `ground_truth` |
| **Trace builder** | Reconstructs per-case traces from correlation IDs (`X-Run-Id`, `X-Case-Id`, `X-Step-Id`) |
| **Outcome Verifier / Evaluator** | Queries persisted bank state and trusted baseline independently; checks KYC state invariants, separately identifies trace-assisted process checks, reports success/failure/incomplete; AML deferred. |
| **Dashboard / reports** | Streamlit UI displaying security posture, active guardrails, blocked threats, and budget consumption |

### 5.2 Metric families

**Proposed outcome/compliance metrics (not measurements). KYC uses trusted baseline + persisted state; AML metrics below are deferred.**

| Metric | Definition |
|---|---|
| **Postcondition Pass Rate** | Scenarios satisfying all invariants (`ONB-P1..P6`; `TXM-P1..P6` deferred) / all executed scenarios |
| **Exploit Catch Rate** | Intercepted prompt injections and parameter swaps / all planted attacks |
| **Tipping-Off Violations (deferred)** | Attempts to contact customer after SAR filing (Target: exactly 0) |
| **Sanctions Screening Recall** | Flagged / screened high-risk entities / all ground-truth sanctioned entities |
| **Idempotency Enforcement** | Persisted client count per application across sessions plus duplicate-attempt decisions; counting blocked attempts alone does not prove exactly-once effects. |
| **Escalation Precision** | Cases escalated to human compliance that truly required EDD / all escalations |

**Efficiency (from proxy events, available live)**

| Metric | Definition |
|---|---|
| Cost per case / per run | Sum of LLM cost, grouped by agent |
| Tokens per case | Input and output separately |
| LLM calls per case | Proxy for reasoning depth or looping |
| Tool calls per case | And per tool, plus error rate |
| End-to-end latency | `case_started` to `case_closed`, p50/p95 |
| Time in LLM vs. tools vs. human queue | Latency breakdown |

**Reliability and behavior**

| Metric | Definition |
|---|---|
| Retry rate | Per agent, per fault type |
| Policy denial rate | Blocked tool calls by agent |
| Loop detection | Cases with repeated identical tool calls |
| Recovery rate | Fault-injected calls where the agent still reached a correct outcome |

**Comparison (across runs)**

Same seed with different models, prompts or authority limits gives directly comparable runs. The dashboard should support run-vs-run diffs on every metric above.

### 5.3 Why ground truth lives here

Keeping the answer key out of the simulation prevents accidental leakage into prompts, tool responses or logs the agents can see. The evaluator reads it only after `run_finished`.

---

## 6. Event contract

The only interface between the simulation/proxy and monitoring. Version it (`schema_version`).

```json
{
  "schema_version": "proposed-1.0",
  "event_id": "uuid",
  "ts": "2026-10-03T10:15:42.123Z",
  "type": "llm_call",
  "run_id": "run_0042",
  "contract_id": "contract_0042",
  "policy_version": "sha256:example-policy",
  "feed_version": "sha256:example-feed",
  "action_id": "action_0004",
  "case_id": "APP-0001",
  "agent_id": "onboarding-agent",
  "step_id": 4,
  "parent_span_id": "span_3",
  "payload": {
    "model": "claude-sonnet-4-6",
    "input_tokens": 1820,
    "output_tokens": 410,
    "latency_ms": 2310,
    "status": 200,
    "stop_reason": "tool_use",
    "cost_usd": 0.0116,
    "decision": "ALLOW",
    "reason_code": "checks_passed",
    "reserved_cost_usd": 0.02,
    "fault_injected": false
  }
}
```

**Event types**

| Type | Emitted by | Key payload |
|---|---|---|
| `run_started` / `run_finished` | Orchestrator | Config, seed, agent versions, model names |
| `case_started` / `case_closed` | Orchestrator | Final status, resolution, escalated flag |
| `llm_call` | LLM proxy | As above |
| `tool_call` | Tool proxy | Sanitized tool/action fields, decision, receipt reference, latency and execution status |
| `policy_denied` | Tool proxy | Agent, tool, rule |
| `fault_injected` | Proxy | Fault type, target |
| `human_decision` | Authorized review queue (stub labelled in tests) | Action/contract/policy/state-bound approval reference, reviewer, expiry and decision |
| `verification_result` | Independent verifier | Status, per-check evidence source, snapshot/verifier version and failed/incomplete invariant IDs |

Examples are illustrative, not a implemented schema. The canonical required envelope is in
`architecture-contract.md`; both architecture documents must use that envelope when implemented.
Only sanitized allowlisted fields are stored/exported. Large raw prompts/results do not go
to blobs by default. Verification, semantic assessments and policy/intervention events need
explicit records with evidence-source and version references.

---

## 7. Run lifecycle

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant G as Generator
    participant A as Agent
    participant P as Proxy
    participant L as LLM API
    participant T as Tool server
    participant B as Event log
    participant E as Evaluator
    participant S as Bank State (read-only verifier path)

    O->>G: generate(seed, config)
    G-->>O: datasets (ground truth sealed)
    O->>B: run_started
    loop each case
        O->>B: case_started
        O->>A: work on case
        A->>P: LLM request (+ correlation headers)
        P->>P: hard checks, reserve budget, required semantic gate
        P->>L: forward only if permitted
        L-->>P: response
        P->>P: inspect output, reconcile usage
        P-->>A: sanitized response
        P--)B: llm_call
        A->>P: tool request
        P->>P: hard/state/approval checks, semantic gate, durable intent
        P->>T: forward only if permitted
        T->>S: atomic business write + effect receipt
        T-->>P: result
        P->>P: inspect output, persist sanitized result
        P-->>A: sanitized result
        P--)B: tool_call
        O->>B: case_closed
    end
    O->>B: run_finished
    E->>B: read events
    E->>S: read fenced, consistent persisted-state snapshot
    E->>E: compare trusted baseline, state, counts and required process evidence
    E->>B: VERIFIED_SUCCESS / FAILED_POSTCONDITIONS / VERIFICATION_INCOMPLETE
```

---

## 8. Suggested repository layout

```
ai-control-layer/
├── data/                     # Mock banking environment & generator (docs/mock-data-spec.md)
│   ├── generate.py           # Implemented stdlib generator, seed 2026
│   ├── rules.py              # Implemented mock AML rules; sharing is not independent rule validation
│   ├── report.py             # HTML explorer for generated dataset
│   ├── bank.db               # SQLite database read/written by agent tools
│   ├── ground_truth.json     # Sealed answer key read ONLY by the Outcome Verifier
│   └── documents/            # Mock applicant OCR files (passports, proof of address)
├── sim/                      # Monitored banking simulation (docs/use-cases.md)
│   ├── agents/               # Planned KYC agent; AML deferred
│   ├── tools/                # SQLite tool implementations (sanctions, registry, bank ops)
│   ├── human_queue/          # Simulated compliance desk stub
│   └── orchestrator.py       # Planned trusted contract + KYC scenario driver
├── proxy/                    # Synchronous AI Control Layer (docs/application-documentation.md)
│   ├── gateway.py            # FastAPI / LiteLLM reverse proxy
│   ├── policy_engine.py      # In-line auditors: PII, prompt injection, budget, tool permissions
│   ├── policy.yaml           # Centralized configuration with hot-reload support
│   └── emitter.py            # Durable outbox event packager
├── contract/                 # Shared event and task contract schemas
│   └── events.schema.json
├── monitoring/               # Independent evaluation & dashboard
│   ├── ingest/               # Event subscriber / worker
│   ├── store/                # DuckDB / SQLite schema
│   ├── verifier/             # Independent postcondition checker (ONB-P1..P6; TXM deferred)
│   └── dashboard/            # Judge-facing audit ledger UI (docs/dashboard-ui.md)
├── tests/                    # Automated self-testing suite (pytest)
│   ├── test_onboarding.py    # Planned positive/negative KYC and persisted-state tests
│   └── test_gateway.py       # Planned bait/gateway tests; no AML suite claimed
└── docker-compose.yml        # Zero-prep local startup
```

`sim/` and `monitoring/` must not import each other. The verifier queries `ground_truth.json` and `bank.db` independently of agent outputs.

---

## 9. Technology suggestions

| Concern | Simple start | Scale-up option |
|---|---|---|
| Proxy | FastAPI or `mitmproxy`-style custom service | LiteLLM or Envoy-based gateway |
| Event log | JSONL files, SQLite | Kafka / Redpanda |
| Analytics store | DuckDB | ClickHouse / Postgres |
| Document fixture storage | Local filesystem | S3 / MinIO (fixtures only; no raw prompt blobs) |
| Dashboard | Fast Web UI / Streamlit | Grafana |
| Tool servers | FastAPI, or MCP servers | Same |

---

## 10. Suggested build order

Follow `architecture-contract.md` section 6: trusted contract and centralized policy first;
one deterministic KYC gateway-to-persisted-write slice with budgets and durable evidence;
independent state verifier and positive/negative tests; selective live semantic supervision;
then a minimal dashboard, approvals and reload demos. The data generator already exists.
Do not add a second agent before this path is demonstrated. Use **uv** for Python environment,
dependencies and execution; `pyproject.toml` and `uv.lock` are the current setup evidence.

---

## 11. Open decisions

- **Storage implementation:** choose a small durable local store for sanitized evidence; raw prompt/secret retention is not an open default.
- **Live vs. batch metrics:** efficiency metrics can be live; outcome metrics are post-run. Do you want a live view at all?
- **Human queue realism:** fixed delay and perfect accuracy, or noisy reviewers?
- **Scale:** how many applications and transactions/alerts per run? (Mock dataset baseline: ~15 applications, 60 alerts, 9,000 transactions).
