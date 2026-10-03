# Monitored Banking Agents: Target System Architecture

Target design for a simulated banking environment with an autonomous **Client Onboarding** agent, a synchronous **AI Control Layer**, and an independent **Evaluation & Metrics Layer** with sealed ground truth. This architecture is not evidence that the runtime components or complete proxy paths are implemented.

The initial agent-runtime integration is planned for OpenCode only. Its plugin adapter is intended to govern OpenCode-managed tool calls that pass through validated hooks. LLM-provider and MCP proxies are separate optional boundaries; no path is covered merely because it appears in this target diagram. See [application-documentation.md](application-documentation.md) for the OpenCode coverage limits and [use-cases.md](use-cases.md) for the current KYC demo scope.

---

## 1. Design principles

1. **Use explicit integration boundaries.** The initial OpenCode plugin is a runtime adapter; separate provider/MCP proxies are needed for those protocol paths. The agent must not be able to silently bypass a boundary that a deployment claims to enforce.
2. **Coverage is deployment-specific.** A proxy is a single control and observation point only when all relevant traffic is forced through it. The initial OpenCode adapter covers only tool executions shown to pass through its validated hooks; it does not by itself intercept every LLM request, shell effect, or network call.
3. **The monitoring layer is a separate deployable.** It consumes the event stream and shares no code or imports with the monitored agents. The only contract is the event schema (section 6).
4. **Ground truth is sealed.** The simulator knows which faults, injections, and anomalies exist (`data/ground_truth.json`). Agents only see `data/bank.db`. Only the evaluator reads ground truth, after a run.
5. **Replayability is a target.** The planned simulation uses seeded runs, append-only events, and deterministic scripted faults; these are not implemented product capabilities yet.

---

## 2. System overview

The diagram shows the **full-proxy target deployment**, not current coverage. In the initial OpenCode integration, only tool calls that pass through validated plugin hooks are in scope. A direct provider or MCP route remains uncovered unless that route is separately proxied and bypass paths are constrained.

```mermaid
flowchart LR
    subgraph SIM["Monitored Banking Environment"]
        GEN[Data generator<br/>bank.db (SQLite)]
        ORCH[Scenario Runner /<br/>Orchestrator]
        subgraph AGENTS["Monitored Agents"]
            ONB[Client Onboarding<br/>Agent]
            AML[AML Transaction<br/>Monitoring Agent<br/>(deferred)]
        end
        TOOLS[Tool servers<br/>bank.db SQLite tools<br/>OCR, Sanctions, Registry]
        HQ[Compliance / Human<br/>Review Queue]
    end

    subgraph PROXY["AI Control Layer (Proxy)"]
        LLMP[LLM proxy<br/>(Ollama / vLLM / OpenAI)]
        TOOLP[Tool proxy<br/>(In-line guardrails)]
        POL[Synchronous Policy Engine<br/>& Scripted Fault Injector]
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
    BUS --> STORE --> EVAL --> DASH
    GT -. read-only postconditions .-> EVAL
```

---

## 3. Banking Simulation Environment

Runs independently of downstream monitoring. Powered by the deterministic mock dataset defined in `docs/mock-data-spec.md`.

### 3.1 Data generator (`data/bank.db` & `data/ground_truth.json`)

| Item | Detail |
|---|---|
| Generator | Seeded (`random.seed(2026)`, `Faker` seeded 2026). Generates byte-identical SQLite database `data/bank.db`. |
| Volumes | ~150 clients, 250 accounts, 8,000 transactions (90 days history), 15 applications, 60 alerts. |
| Tables | `clients`, `accounts`, `transactions`, `applications`, `alerts`, `company_registry`, `sanctions_list`, `pep_list`, `documents`. |
| Planted Anomalies | Sanctions hits, PEP flags, prompt injections in OCR/memos, expired passports, structuring cash deposits, rapid movement via high-risk jurisdictions. |
| Sealed Ground Truth | Written to `data/ground_truth.json` (contains actual labels, expected dispositions, and correct resolutions; unseen by agents). |

### 3.2 Monitored Agents (defined in `docs/use-cases.md`)

| Agent | Objective | Key Tools | Authority & Guardrails |
|---|---|---|---|
| **Client Onboarding Agent** | Triage and decide onboarding applications (`APP-…`) | `read_application`, `read_documents`, `extract_fields`, `check_registry`, `screen_sanctions`, `compute_risk`, `create_client`, `request_more_docs`, `escalate_edd`, `reject_application` | `create_client` writes to `clients` & `accounts`; requires prior sanctions screening and unexpired docs. |
| **AML Transaction Monitoring Agent (deferred)** | Future investigation and disposition of AML alerts (`ALR-…`) | `get_alert`, `get_transactions`, `get_customer_profile`, `get_counterparty_info`, `close_alert`, `file_sar`, `freeze_account`, `contact_customer` | Future scope: `close_alert`, `file_sar`, `freeze_account` mutate state; `contact_customer` forbidden after SAR filing (anti-tipping-off). |

In the full-proxy target deployment, agents use Ollama with `base_url = <proxy>` and each request carries correlation headers (section 4.3). For the initial OpenCode integration, tool-hook coverage and provider-proxy coverage are separate; configuring one does not imply the other.

### 3.3 Tool implementation

Plain Python functions executing over `data/bank.db` (SQLite). Actions with side-effects (`create_client`, `freeze_account`, `file_sar`) modify database rows, allowing the Outcome Verifier to validate persisted state directly.

### 3.4 Orchestrator / Scenario Runner

For the current KYC-only demo, the planned runner drives `ONB-01`..`ONB-15`, sets `run_id` and `case_id`, attaches Task Contracts, and enables scripted faults for automated testing. The 13 AML scenarios (`TXM-01`..`TXM-13`) are deferred with the AML agent.

### 3.5 Human Review Queue (Compliance Desk)

Receives escalations (`escalate_edd`, approval requests) and records human decisions. Allows measuring escalation precision and human-in-the-loop latency.

---

## 4. Proxy layer

In a full-proxy deployment, route the relevant agent traffic through the proxy to obtain synchronous enforcement and per-call telemetry. The initial OpenCode adapter does not by itself provide this complete topology; uncovered paths must be identified explicitly.

### 4.1 LLM proxy

- Exposes an API-compatible endpoint (same request/response shape as the upstream provider), so compatible clients need no code changes beyond `base_url`.
- Forwards to the real provider, streams responses back.
- Records, per call: model, token counts, latency, time-to-first-token, status code, retries, stop reason, and tool-use blocks. Raw prompts/completions are excluded by default; any retained content requires explicit redaction, access, and retention controls. Prefer sanitized summaries or carefully designed fingerprints over content-bearing blobs.
- Computes cost from a price table (kept in the proxy config, not in agents).

### 4.2 Tool proxy

- In a proxy-based deployment, fronts the configured tool servers. Agents must have no direct route around it for the deployment to claim protocol-level coverage.
- Records: tool name, arguments, result (or hash plus pointer), latency, success or error, bytes returned.
- Can enforce permission scopes per agent identity (e.g. the current KYC agent may not call AML-only tools; a future AML agent may not call `contact_customer` after filing a SAR). A denied call is itself an event.

### 4.3 Correlation headers

Every request routed through the full proxy deployment includes:

| Header | Meaning |
|---|---|
| `X-Run-Id` | One simulation run |
| `X-Case-Id` | One case being worked (`APP-0001`, `ALR-0002`, ...) |
| `X-Agent-Id` | Which agent (`onboarding_agent`, `aml_agent`) |
| `X-Step-Id` | Monotonic step counter within the case |
| `X-Parent-Span-Id` | Links a call to the step that triggered it |

These are what let the monitoring layer rebuild a full trace without ever seeing agent internals.

### 4.4 Synchronous Policy Enforcement & Scripted Faults

The proxy evaluates in-line guardrails before requests reach upstream LLMs or tool execution:
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

The proxy writes events **asynchronously** to the event log/queue (`Queue 1`) so telemetry persistence never slows down synchronous enforcement. If the log is unavailable, the proxy buffers locally (e.g. to a local SQLite table or JSONL file) and flushes later.

---

## 5. Monitoring and metrics layer

A separate service (own repo or package, own process). Inputs: the event log and the sealed ground truth (`data/ground_truth.json`). It never calls agents, the orchestrator, or tool servers directly.

### 5.1 Components

| Component | Responsibility |
|---|---|
| **Event log / bus** | Append-only record of all events. Start with SQLite / local JSONL; upgrade to Kafka/Redpanda if live streaming cluster is required |
| **Ingestor** | Validates events against the schema, writes to the analytics store |
| **Analytics store** | DuckDB or SQLite (Postgres / ClickHouse for large scale). Tables: `llm_calls`, `tool_calls`, `lifecycle`, `faults`, `ground_truth` |
| **Trace builder** | Reconstructs per-case traces from correlation IDs (`X-Run-Id`, `X-Case-Id`, `X-Step-Id`) |
| **Outcome Verifier / Evaluator** | Joins traces to sealed `ground_truth.json` post-run and asserts postconditions (`ONB-P1..P6`, `TXM-P1..P6`) |
| **Dashboard / reports** | Streamlit UI displaying security posture, active guardrails, blocked threats, and budget consumption |

### 5.2 Metric families

**Outcome & Compliance (verified against `ground_truth.json`)**

| Metric | Definition |
|---|---|
| **Postcondition Pass Rate** | Scenarios satisfying all invariants (`ONB-P1..P6`, `TXM-P1..P6`) / all executed scenarios |
| **Exploit Catch Rate** | Intercepted prompt injections and parameter swaps / all planted attacks |
| **Tipping-Off Violations** | Attempts to contact customer after SAR filing (Target: exactly 0) |
| **Sanctions Screening Recall** | Flagged / screened high-risk entities / all ground-truth sanctioned entities |
| **Idempotency Enforcement** | Blocked duplicate write attempts (`create_client`, `file_sar`) / all duplicate attempts |
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
  "schema_version": "1.0",
  "event_id": "uuid",
  "ts": "2026-10-03T10:15:42.123Z",
  "type": "llm_call",
  "run_id": "run_0042",
  "case_id": "ALR-0002",
  "agent_id": "aml_agent",
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
    "request_ref": null,
    "response_ref": null,
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
| `tool_call` | Tool proxy | Tool name, args ref, result ref, latency, ok/error |
| `policy_denied` | Tool proxy | Agent, tool, rule |
| `fault_injected` | Proxy | Fault type, target |
| `human_decision` | Human queue stub | Decision, delay |

Raw prompts and tool results are not persisted by default. If a task explicitly requires retaining content, apply secret/PII redaction, least-privilege access, and a retention limit before storing it; an opaque `*_ref` does not make sensitive content safe. Default event fields should be allowlisted metadata and bounded sanitized summaries.

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

    O->>G: generate(seed, config)
    G-->>O: datasets (ground truth sealed)
    O->>B: run_started
    loop each case
        O->>B: case_started
        O->>A: work on case
        A->>P: LLM request (+ correlation headers)
        P->>L: forward
        L-->>P: response
        P-->>A: response
        P--)B: llm_call
        A->>P: tool request
        P->>T: forward (if permitted)
        T-->>P: result
        P-->>A: result
        P--)B: tool_call
        O->>B: case_closed
    end
    O->>B: run_finished
    E->>B: read events
    E->>E: join with ground truth, compute metrics
```

---

## 8. Suggested repository layout

```
ai-control-layer/
├── data/                     # Mock banking environment & generator (docs/mock-data-spec.md)
│   ├── generate.py           # Deterministic generator (Faker / random seed 2026)
│   ├── rules.py              # AML rules engine (R-1..R-6) used by generator & verifier
│   ├── report.py             # HTML explorer for generated dataset
│   ├── bank.db               # SQLite database read/written by agent tools
│   ├── ground_truth.json     # Sealed answer key read ONLY by the Outcome Verifier
│   └── documents/            # Mock applicant OCR files (passports, proof of address)
├── simulation/               # Monitored banking simulation (docs/use-cases.md)
│   ├── agents/               # OpenCode/KYC integration initially; future agents deferred
│   ├── tools/                # SQLite tool implementations (sanctions, registry, bank ops)
│   ├── human_queue/          # Simulated compliance desk stub
│   └── orchestrator.py       # Initial KYC scenarios (ONB-01..15); AML deferred
├── proxy/                    # Synchronous AI Control Layer (docs/application-documentation.md)
│   ├── gateway.py            # FastAPI / LiteLLM reverse proxy
│   ├── policy_engine.py      # In-line auditors: PII, prompt injection, budget, tool permissions
│   ├── policy.yaml           # Centralized configuration with hot-reload support
│   └── emitter.py            # Non-blocking async event packager
├── contract/                 # Shared event and task contract schemas
│   └── events.schema.json
├── monitoring/               # Independent evaluation & dashboard
│   ├── ingest/               # Event subscriber / worker
│   ├── store/                # DuckDB / SQLite schema
│   ├── verifier/             # Independent KYC postcondition checker initially; AML deferred
│   └── dashboard/            # Judge-facing audit ledger UI (docs/dashboard-ui.md)
├── tests/                    # Automated self-testing suite (pytest)
│   ├── test_onboarding.py    # Planned KYC positive & negative scenarios
│   └── test_aml.py           # Future AML scenarios (deferred)
└── docker-compose.yml        # Zero-prep local startup
```

`simulation/` and `monitoring/` must not import each other. The verifier queries `ground_truth.json` and `bank.db` independently of agent outputs.

---

## 9. Technology suggestions

| Concern | Simple start | Scale-up option |
|---|---|---|
| Proxy | FastAPI or `mitmproxy`-style custom service | LiteLLM or Envoy-based gateway |
| Event log | JSONL files, SQLite | Kafka / Redpanda |
| Analytics store | DuckDB | ClickHouse / Postgres |
| Blob storage | Local filesystem | S3 / MinIO |
| Dashboard | Fast Web UI / Streamlit | Grafana |
| Tool servers | FastAPI, or MCP servers | Same |

---

## 10. Suggested build order

1. **Contract:** write `events.schema.json` first.
2. **Generator:** seeded mock banking data (`data/generate.py`, `data/rules.py`) with planted faults and sealed ground truth.
3. **Single agent through the proxy:** Client Onboarding Agent (e.g. `ONB-01` baseline) plus LLM proxy plus JSONL/SQLite emitter. Confirm events appear.
4. **KYC tool path:** route one KYC side-effecting tool through the OpenCode adapter and deterministic policy before expanding tool coverage; provider/MCP proxies remain separate.
5. **Monitoring ingest and store:** load sanitized events into local storage and build per-case traces.
6. **Evaluator:** independently verify KYC postconditions against trusted persisted state and sealed ground truth.
7. **Dashboard:** audit ledger UI (`docs/dashboard-ui.md`) with Attack Console and trajectory/outcome replay.
8. **KYC fault injection and policy rules:** run the in-scope onboarding scenarios with positive and negative controls. AML/TXM work remains deferred.

---

## 11. Open decisions

- **Prompt storage:** log full prompts (best for debugging, large) or hashes only?
- **Live vs. batch metrics:** efficiency metrics can be live; outcome metrics are post-run. Do you want a live view at all?
- **Human queue realism:** fixed delay and perfect accuracy, or noisy reviewers?
- **Scale:** how many applications and transactions/alerts per run? (Mock dataset baseline: ~15 applications, 60 alerts, 8,000 transactions).
