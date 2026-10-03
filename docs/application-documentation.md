# Local Agent Gateway & Control Plane: Architecture

A lightweight, locally runnable **interception, persistence, and consumer gateway** designed to run on the same system (or sidecar) as the agentic loop. 

Unlike heavy distributed enterprise platforms, this architecture provides a **clean, modular, and maintainable local control layer** that unifies:
1. **Synchronous Action Interception & Auditing** (inspect, evaluate, block, or transparently route LLM and MCP/tool actions).
2. **Decoupled Document Persistence** (buffered, non-blocking append to a JSON/MongoDB document store with reliable consumer queueing and retry management).
3. **Asynchronous Consumer & Risk Intelligence** (pluggable metric calculation, agent trajectory risk grading, and a **dynamic feedback loop** that tightens interception rules in real-time).

---

## 1. Core Architectural Overview

The system operates across three tightly scoped layers connected by asynchronous queues and a dynamic policy feedback channel:

```mermaid
flowchart TB
    subgraph LOCAL_HOST["Local Host / Agent Environment"]
        subgraph AGENT_ENV["Agent Execution Plane"]
            AGENT["OpenCode Runtime<br/>(initial planned integration)"]
            OCADAPTER["OpenCode Plugin Adapter<br/>(tool-execution hooks)"]
            AGENT <--> OCADAPTER
        end

        subgraph LAYER1["Layer 1: Interception Layer (Gateway & Policy Guard)"]
            direction TB
            ENTRY["Gateway Listener<br/>(LLM HTTP / MCP JSON-RPC / Tool Proxy)"]
            AUDIT_PIPE["Synchronous Policy Pipeline<br/>(In-Process Callbacks & Webhook Auditors)"]
            ROUTER["Transparent Router & Fallback<br/>(Primary Provider -> Fallback Provider)"]
            UPSTREAM[("Configured Upstream<br/>LLM Providers / MCP Servers")]
            PACKER["Event Normalizer & Packager"]
            Q1[("Queue 1: Ingestion Buffer<br/>(In-Memory / Local Queue)")]

            ENTRY --> AUDIT_PIPE
            AUDIT_PIPE -- "BLOCK / REJECT" --> ENTRY
            AUDIT_PIPE -- "ALLOW" --> ROUTER
            OCADAPTER -- "Tool action" --> AUDIT_PIPE
            AUDIT_PIPE -- "Decision / approval state" --> OCADAPTER
            OCADAPTER --> PACKER
            ROUTER <--> UPSTREAM
            ROUTER --> PACKER
            PACKER --> Q1
        end

        subgraph LAYER2["Layer 2: Persistence Layer (Document Store & Reliable Dispatcher)"]
            direction TB
            PERSIST_WORKER["Persistence Worker"]
            DOC_STORE[("Document Store<br/>(MongoDB / JSON Append Store)")]
            Q2_DISPATCHER["Consumer Dispatcher & Retry Supervisor"]
            Q2[("Queue 2: Consumer Queue<br/>(With Exponential Retries & DLQ)")]

            Q1 --> PERSIST_WORKER
            PERSIST_WORKER --> DOC_STORE
            PERSIST_WORKER --> Q2_DISPATCHER
            Q2_DISPATCHER --> Q2
        end

        subgraph LAYER3["Layer 3: Consumer Layer (Risk Intelligence & Observability)"]
            direction TB
            CONSUMER_RUNTIME["Consumer Manager"]
            subgraph PLUGINS["Pluggable Consumer Modules"]
                P_METRICS["Metrics & Cost Observer"]
                P_LOOP["Loop & Anomaly Detector"]
                P_RISK["Trajectory Risk Grader"]
                P_CUSTOM["Custom Webhook / Callback"]
            end
            SINKS["Observability Sinks<br/>(Dashboard / Logs / Exports)"]
            FEEDBACK_GEN["Policy Feedback Controller"]

            Q2 --> CONSUMER_RUNTIME
            CONSUMER_RUNTIME --> PLUGINS
            PLUGINS --> SINKS
            P_RISK & P_LOOP --> FEEDBACK_GEN
        end

        FEEDBACK_GEN -. "Dynamic Policy Update<br/>(Escalate Rules / Strict Mode / Block)" .-> AUDIT_PIPE
    end

    AGENT <-->|Optional provider/MCP proxy path| ENTRY
```

The OpenCode plugin path and the optional HTTP/MCP gateway path are distinct. The plugin sends hooked tool actions to policy and receives a decision; it does not imply that OpenCode provider traffic or all operating-system effects pass through the gateway.

### Architectural Highlights

- **Local-First & Resource-Lean:** Runs as an embedded process or lightweight local daemon on the agent's host. No heavy distributed message brokers (Kafka/Zookeeper) or multi-tier object storage clusters required.
- **Fail-Safe & Low Latency:** The hot interception path performs fast deterministic evaluations and transparent routing without waiting for database operations or complex analytics.
- **Bi-Directional Risk Supervision:** Consumers don't just passively log metrics—they calculate compound risk and actively feed updated policies back into the interception layer to constrain rogue agents dynamically.

---

## 2. Layer 1: Interception Layer (Gateway & Policy Enforcement)

The Interception Layer sits directly in the path between the agent and external resources (LLMs, MCP servers, tools). It is responsible for intercepting requests, evaluating actions against synchronous audit rules, handling transparent upstream routing with fallback, and pushing normalized action records to the persistence layer.

```mermaid
sequenceDiagram
    autonumber
    participant Agent as Agent / Tool Caller
    participant Interceptor as Interception Gateway
    participant Auditor as Synchronous Auditor Plugins
    participant Router as Transparent Router
    participant Upstream as Primary / Fallback Provider
    participant Q1 as Queue 1 (Persistence)

    Agent->>Interceptor: Request (LLM prompt, MCP tool call, egress action)
    Interceptor->>Auditor: Evaluate Action (Context, Tool, Arguments)
    
    alt Action is Blocked (Policy Violation)
        Auditor-->>Interceptor: Decision: BLOCK (reason, violation_code)
        Interceptor-->>Agent: Immediate Error Response (403 / MCP Error)
        Interceptor->>Q1: Publish Blocked Action Event
    else Action is Approved
        Auditor-->>Interceptor: Decision: ALLOW (or REDACT / MODIFY)
        Interceptor->>Router: Forward Action
        Router->>Upstream: Forward to Primary Provider
        alt Primary Fails (timeout / 5xx / rate limit)
            Router->>Upstream: Fallback to Secondary Provider
        end
        Upstream-->>Router: Response (stream or payload)
        Router-->>Agent: Return Response to Agent
        Interceptor->>Q1: Publish Completed Action Event
    end
```

### 2.1 Interception Scope

The architecture supports three separate interception boundaries; a deployment does not automatically cover all of them. The initial agent-runtime integration is planned for **OpenCode only**. The proxy options below are distinct integration paths, not capabilities supplied automatically by an OpenCode plugin.

1. **LLM API Proxy:** Exposes standard OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) compatible endpoints. Supports both buffered requests and streaming Server-Sent Events (SSE).
2. **MCP (Model Context Protocol) Proxy:** Intercepts JSON-RPC tool invocations (`tools/call`, `resources/read`, `prompts/get`) between agents and MCP servers.
3. **HTTP Egress / In-App Tool Callbacks:** Acts as an egress proxy for REST tools, or provides a lightweight programmatic SDK wrapper for in-process function execution.

#### 2.1.1 Initial Runtime Adapter: OpenCode (Planned)

The OpenCode adapter is the runtime-specific edge of the control layer; policy evaluation and the Task Contract remain in the shared control plane. The first integration will use an OpenCode plugin's `tool.execute.before` and `tool.execute.after` hooks from the [OpenCode plugin API](https://opencode.ai/docs/plugins/), subject to compatibility testing against the pinned OpenCode version. The current development environment reports OpenCode `v2.0.22`; that version has not yet been validated with this adapter.

The enforcement described here applies only to actions that actually pass through the hooked OpenCode tool-execution path. A `bash` call exposes a command string, not a complete mediation point for all subprocess, filesystem, or network effects. Provider traffic needs a separately configured LLM proxy. MCP tools exposed through OpenCode must be checked to confirm the hook executes before their side effect; protocol-level MCP enforcement requires routing them through an MCP proxy. Direct routes, other plugins, or a disabled adapter may bypass these hooks. Where those paths matter, constrain credentials and OS/network access, and report any unmediated path as uncovered rather than claiming comprehensive protection. The [initial coverage inventory and acceptance plan](intercept/opencode-adapter-plan.md) records the planned scope; coverage remains unverified until the listed tests pass.

Before a hooked tool executes, the adapter will normalize its name and proposed arguments, attach trusted run/contract context available to the integration, and synchronously request a decision from the control layer. The shared policy evaluates task-scoped tool permissions, argument constraints, approvals, and run budgets, and returns the policy version used. Budget accounting and hard constraints remain deterministic and centralized; semantic supervision cannot override a deny, missing approval, or exhausted budget. The adapter must enforce hard policy constraints before execution:

- `ALLOW`: continue with the checked action.
- `BLOCK`: stop the invocation before the tool can run.
- `REDACT` / `MODIFY`: apply only an explicit policy-produced argument change supported by the OpenCode hook API.
- `REQUIRE_APPROVAL`: pause until an authorized approval is obtained for the specific action; approval is not an immediate allow.

After execution, the adapter will observe the tool outcome and send a sanitized event to the asynchronous ingestion path. Events should use an allowlist of necessary fields; raw arguments, tool results, authorization headers, and unbounded error text are excluded by default. Apply secret/PII scrubbing and bounded summaries before enqueueing. Policy evaluation remains synchronous; persistence must not delay the decision. The exact hook payload, supported argument mutation, blocking behavior, and any ability to alter results must be verified for the pinned OpenCode version. Do not claim output redaction if a hook can only observe the result; enforce it at a response boundary that supports modification.

Event enqueueing records an observed tool outcome; it is not independent outcome verification. For a workflow with side effects, a separate verifier must query trusted external state, check explicit postconditions (including uniqueness where needed), and distinguish verified success, failed postconditions, and verification that could not be completed.

```mermaid
sequenceDiagram
    participant OC as OpenCode runtime
    participant Adapter as OpenCode plugin adapter
    participant Policy as Control-layer policy
    participant Tool as OpenCode tool
    participant Queue as Async event ingestion

    OC->>Adapter: tool.execute.before(tool, arguments, run context)
    Adapter->>Policy: Evaluate normalized action against Task Contract
    Policy-->>Adapter: Decision + policy version
    alt BLOCK or approval rejected/timed out
        Adapter-->>OC: Stop execution
    else REQUIRE_APPROVAL
        Adapter-->>OC: Hold invocation while approval is pending
        Adapter->>Policy: Validate authorized approval for this action/version
        Policy-->>Adapter: Approval granted or denied
        alt Approval denied
            Adapter-->>OC: Stop execution
        else Approval granted
            Adapter-->>OC: Continue with approved arguments
            OC->>Tool: Execute tool
            Tool-->>OC: Result or failure
            OC->>Adapter: tool.execute.after(outcome)
            Adapter-->>Queue: Enqueue sanitized action event
        end
    else ALLOW or permitted modification
        Adapter-->>OC: Continue with permitted arguments
        OC->>Tool: Execute tool
        Tool-->>OC: Result or failure
        OC->>Adapter: tool.execute.after(outcome)
        Adapter-->>Queue: Enqueue sanitized action event
    end
```

Other agent runtimes may later use separate adapters to translate their hooks into the shared action and decision contracts. No other runtime adapter is part of the initial implementation scope.

### 2.2 Synchronous Action Auditing & Pluggable Policies

Before any action is sent to an external provider or executed on a tool, it passes through an extensible **Action Auditor Pipeline**.

#### The Decision Contract
Each auditor plugin evaluates the proposed action and returns a deterministic evaluation:
- `ALLOW`: The action complies with policies; forward immediately.
- `BLOCK`: The action violates policy; reject immediately and return a structured error to the agent.
- `REDACT`: Sanitize sensitive fields (e.g., tokens, PII) in arguments before forwarding.
- `REQUIRE_APPROVAL`: Pause execution until an external local confirmation is provided.
- `ALERT`: Emit a high-priority warning and increase session risk score. It is not authorization and never overrides another control; the action proceeds only if the resolved policy otherwise permits it.

The pipeline uses deterministic conflict resolution, not first-match allow: hard `BLOCK` (including exhausted budget) takes precedence over `REQUIRE_APPROVAL`, which takes precedence over `ALLOW`; `ALERT` is additive and never grants permission. Apply configured argument transformations in order, then re-run hard checks against the final payload before execution. Bind approval to the finalized action, run, policy version, and active intervention/overlay version; a changed action or applicable policy overlay invalidates the approval and requires a new decision. Record every auditor decision, including alerts and denials, in sanitized audit evidence.

#### Dual Plugin Delivery Mechanisms
To make the system easily extendable and polyglot-friendly, two plugin integration modes are supported:
1. **In-Application Callbacks (In-Process):** Fast, zero-overhead Python/Go functions implementing a standard `ActionAuditor` interface. Ideal for fast local regex, allowlists, argument bounds checking, and token budgets.
2. **Webhook Callbacks (HTTP/gRPC):** Configured HTTP endpoints that receive the action payload and return a decision JSON. Ideal for running isolated policy checkers, local LLM-based guard models, or existing external policy daemons.

#### Example Auditor Configuration (`gateway_config.yaml`)
```yaml
interception:
  port: 8080
  strict_mode: false # Can be dynamically toggled by Layer 3 feedback
  
  auditors:
    # 1. In-process deterministic validator (ordered chain)
    - name: "tool-allowlist"
      type: "in_process"
      handler: "core.auditors.allowlist:ToolAllowlistAuditor"
      config:
        allowed_tools: ["search_docs", "calculate_margin", "query_portfolio"]
        block_action: "reject"

    # 2. Secret & PII scanner
    - name: "credential-shield"
      type: "in_process"
      handler: "core.auditors.security:CredentialShieldAuditor"
      config:
        patterns: ["aws_secret", "private_key", "password"]

    # 3. External Webhook for complex or polyglot rule checks
    - name: "compliance-evaluator"
      type: "webhook"
      endpoint: "http://127.0.0.1:9090/audit/action"
      timeout_ms: 150
```

#### Dynamic Configuration Hot-Reloading
The policy engine continuously monitors `gateway_config.yaml` / `policy.yaml` for file modification events (via inotify / file watcher). When configuration files are modified (e.g. judges adjusting thresholds, toggling allowlists, or updating token budgets during evaluation):
- Updated policies are immediately validated and swapped in-memory.
- A validated policy snapshot is atomically activated for new runs; each in-flight run remains bound to the policy version in its Task Contract.
- Any authorized emergency tightening for an in-flight run is an explicit, scoped, versioned, monotonic overlay/intervention; it may tighten but never relax the run's bound hard constraints. Record its issuer, scope, reason, lifetime, and version. Re-evaluate affected pending actions and invalidate approvals that no longer match the active policy/overlay versions.
- Invalid or partially written configuration is rejected; the last valid snapshot remains active and a sanitized reload-failure event is emitted.
- A `policy_reloaded` event records the old and new policy version hashes and activation outcome.

### 2.3 Transparent Backend Routing & Fallback

The interception layer decouples the agent from specific hardcoded LLM endpoints or MCP servers, enabling resilient transparent fallback:

- **Provider Routing:** Maps abstract model names requested by the agent (e.g., `primary-reasoning-model`) to specific backends (e.g., local Ollama, vLLM, Anthropic, OpenAI).
- **Automated Fallback:** If the primary backend returns a `429 (Rate Limit)`, `5xx (Server Error)`, or times out, the router automatically fails over to the configured secondary backend without breaking the agentic loop.
- **MCP Backend Gateway:** Maps tool calls to either local stdio MCP processes, local HTTP/SSE servers, or fallback endpoints.

```yaml
routing:
  llm:
    providers:
      anthropic_primary:
        type: "anthropic"
        base_url: "https://api.anthropic.com"
        api_key_env: "ANTHROPIC_API_KEY"
      ollama_local_fallback:
        type: "openai_compatible"
        base_url: "http://127.0.0.1:11434/v1"
        api_key_env: "EMPTY"

    routes:
      "claude-3-5-sonnet":
        primary: "anthropic_primary"
        fallback: "ollama_local_fallback"
        timeout_seconds: 30
        retry_on_status: [429, 500, 502, 503, 504]
```

### 2.4 Event Packaging & Queue Handoff

Once an action completes (or is blocked), the gateway packages the complete interaction into an **Application-Level Action Event** and asynchronously enqueues it into `Queue 1`. 

**Critical Invariant:** Enqueuing to `Queue 1` is completely non-blocking (in-memory ring buffer or fast local queue). The agent request path **never** waits for database disk writes.

---

## 3. Layer 2: Persistence Layer (Document Store & Queueing Pipeline)

The Persistence Layer ensures that every action is reliably preserved for local querying and auditing, while safely decoupling storage workloads from asynchronous consumer processing.

```mermaid
flowchart LR
    Q1[("Queue 1: Ingest Buffer")] --> WORKER["Persistence Worker"]
    WORKER -->|"Append (Batch / Bulk Write)"| MONGO[("Document Store<br/>(MongoDB / Local JSON)")]
    WORKER -->|"Enqueue Normalized Event"| DISPATCHER["Reliable Consumer Dispatcher"]
    DISPATCHER --> Q2[("Queue 2: Consumer Channel")]
    
    subgraph RETRY_MANAGEMENT["Failure Supervision"]
        Q2 --> CONSUMERS["Consumer Modules"]
        CONSUMERS -- "Ack" --> DONE[("Completed")]
        CONSUMERS -- "Nack / Crash" --> RETRY_LOGIC["Exponential Backoff Retry"]
        RETRY_LOGIC --> Q2
        RETRY_LOGIC -- "Max Attempts Exceeded" --> DLQ[("Dead Letter Queue (DLQ)")]
    end
```

### 3.1 Document Store (MongoDB / Append-Optimized Store)

Agent actions represent rich, dynamic, nested documents (prompts, tool parameters, structured responses, JSON-RPC payloads). **MongoDB (or a local document store)** is the ideal persistence target because:
1. **Schema Agnostic & Natural JSON:** Arbitrary tool arguments and provider-specific attributes map 1:1 without relational schema migrations.
2. **High-Throughput Append Workloads:** MongoDB handles rapid append-only writes via time-bucketed collections or capped collections with minimal write latency.
3. **Rich Local Querying:** Easily query action histories by session, trace, tool name, or risk attributes for local debugging and dashboard exploration.

#### Indexing Strategy
```javascript
// Primary query patterns for local observability
db.agent_actions.createIndex({ "trace_id": 1, "ts": 1 });
db.agent_actions.createIndex({ "session_id": 1, "ts": 1 });
db.agent_actions.createIndex({ "action_type": 1, "status": 1 });
db.agent_actions.createIndex({ "risk_metadata.risk_level": 1 });
```

### 3.2 Reliable Queue 2 & Consumer Supervision

After the persistence worker commits an event to the document store, it pushes the event to **Queue 2** for downstream evaluation:
- **Decoupled Failure Domains:** A failure, bug, or slow execution in a consumer module never impacts the persistence layer or the interception gateway.
- **At-Least-Once Delivery:** Events in Queue 2 require explicit consumer acknowledgment (`ACK`).
- **Retry Mechanism with Exponential Backoff:** If a consumer fails or times out (e.g., an external webhook consumer is temporarily unreachable), the dispatcher retries delivery with jittered exponential backoff.
- **Dead Letter Queue (DLQ):** Messages exceeding `max_retries` are moved to a local dead-letter store for offline inspection without stalling the consumer pipeline.

---

## 4. Layer 3: Consumer Layer (Risk Intelligence & Feedback Control)

The Consumer Layer executes in the background. It consumes persisted events from Queue 2, calculates rolling metrics, evaluates agent behavior across multiple steps, and—critically—**exerts dynamic feedback control back onto Layer 1**.

```mermaid
flowchart TD
    Q2[("Queue 2 Events")] --> RUNTIME["Consumer Dispatcher"]

    subgraph CONSUMERS["Pluggable Consumer Modules"]
        C1["Cost & Token Accountant<br/>(Calculates burn rate, spend limits)"]
        C2["Loop & Anomaly Detector<br/>(Identifies repetitive or oscillating tool calls)"]
        C3["Trajectory Risk Grader<br/>(Calculates drift, unauthorized access risk)"]
        C4["Webhook Consumer<br/>(External grader / Custom security model)"]
    end

    RUNTIME --> C1 & C2 & C3 & C4

    subgraph OUTPUTS["Observability & Analytics"]
        METRICS_STORE["Local Metrics / Time-Series"]
        UI_DASH["Local Web Dashboard"]
    end

    C1 & C2 & C3 --> METRICS_STORE --> UI_DASH

    subgraph FEEDBACK_LOOP["Closed-Loop Control"]
        RISK_EVAL["Risk Policy Evaluator"]
        FEEDBACK_CHANNEL[("Dynamic Policy Control Channel<br/>(In-Memory / IPC Bus)")]
        DYNAMIC_CACHE["Layer 1 Dynamic Policy Cache"]

        C2 & C3 & C4 --> RISK_EVAL
        RISK_EVAL -->|"Emit PolicyAdjustmentSignal<br/>(Severity >= THRESHOLD)"| FEEDBACK_CHANNEL
        FEEDBACK_CHANNEL --> DYNAMIC_CACHE
    end
```

### 4.1 Pluggable Consumer Modules

Consumers are designed as isolated, plug-and-play plugins implementing an asynchronous processor interface:
- **In-Process Modules:** Python/Go worker coroutines that run in the background consumer process.
- **Webhook Consumers:** HTTP endpoints that receive streaming batches of events, allowing external tools, evaluation frameworks, or specialized microservices to analyze traces.

#### Reference Consumer Implementations
1. **Loop & Oscillation Detector:** Tracks tool invocations within a rolling window (`trace_id`). If an agent calls the same tool with identical or alternating parameters more than $N$ times, flags an infinite loop risk.
2. **Trajectory & Drift Grader:** Analyzes the sequence of actions against the expected workflow or contract. Detects deviations (e.g., agent was tasked with data retrieval but suddenly attempts file deletion or network discovery).
3. **Spend & Velocity Accountant:** Aggregates token consumption and cost per session. Tracks rate-of-burn to catch runaway recursive agent loops before resource exhaustion.

### 4.2 Dynamic Feedback Loop: Influencing the Interception Layer

Traditional observability is purely passive; by the time a human reads a dashboard, the agent has already executed unwanted actions. 

This architecture introduces a **closed-loop feedback mechanism**:
1. When a consumer (e.g., `Trajectory Risk Grader` or `Loop Detector`) identifies suspicious, risky, or anomalous behavior, it synthesizes a **`PolicyAdjustmentSignal`**.
2. The signal is dispatched over a local fast IPC channel (in-memory state, local event bus, or unix domain socket) to the Interception Layer's **Dynamic Policy Cache**.
3. Layer 1 immediately updates its runtime policy for the affected `session_id` or `agent_id`:
   - **Enforce Strict Allowlist:** Disable optional or potentially dangerous tools.
   - **Enforce Human Approval:** Convert automatic tool execution into `REQUIRE_APPROVAL` mode.
   - **Throttle / Rate-Limit:** Add synthetic delay or limit token budget for the session.
   - **Hard Halt:** Issue a session-wide `BLOCK` for all subsequent LLM or tool requests.

```json
// Example: PolicyAdjustmentSignal emitted by Trajectory Risk Grader
{
  "signal_id": "sig_01J9ZK49B...",
  "ts": "2026-10-03T15:45:00.120Z",
  "target_scope": {
    "session_id": "sess_onboarding_APP0001",
    "agent_id": "client_onboarding_agent"
  },
  "action": "ESCALATE_POLICY",
  "policy_modifications": {
    "strict_mode": true,
    "require_approval_for": ["create_client", "freeze_account", "file_sar"],
    "blocked_tools": ["shell_exec", "eval_code", "contact_customer"]
  },
  "reason": "Anomalous tool call velocity detected (5 attempts in 2 seconds) with parameter drift.",
  "ttl_seconds": 600
}
```

---

## 5. Unified Data Contracts & Interfaces

To maintain clean separation and make the system truly extendable, all components communicate via standardized schemas and programming interfaces.

### 5.1 Application Action Event Envelope (Stored in DB & Queues)

This illustrative envelope is not a license to store raw payloads. Persist only allowlisted metadata and sanitized, bounded summaries by default; exclude raw arguments/results, credentials, authorization headers, and unbounded error text unless a separately approved retention policy explicitly permits a redacted form.

```json
{
  "schema_version": "2.0",
  "event_id": "evt_01J9ZK3Q8W2M5N7R4T6V8X0Y1A",
  "trace_id": "tr_4bf92f3577b34da6a3ce929d0e0e4736",
  "session_id": "sess_kyc_APP0001",
  "case_id": "APP-0001",
  "agent_id": "onboarding_agent",
  "ts": "2026-10-03T15:42:10.500Z",
  "task_contract_id": "contract_APP0001_v1",
  "policy_version": "sha256:synthetic-policy-hash",
  "action_type": "tool_call", 
  "source": "opencode_plugin_adapter",
  "status": "completed", 
  
  "action_details": {
    "name": "create_client",
    "parameters_summary": {
      "application_id": "APP-0001",
      "sensitive_fields": "[REDACTED]"
    },
    "reported_result_summary": {
      "status": "created"
    }
  },

  "interception_metadata": {
    "auditor_decisions": [
      {
        "auditor": "credential-shield",
        "decision": "ALLOW",
        "latency_ms": 0.8
      }
    ],
    "routed_upstream": null,
    "fallback_triggered": false,
    "interception_overhead_ms": 1.2
  },

  "metrics": {
    "input_tokens": 450,
    "output_tokens": 85,
    "latency_ms": 142.5,
    "tool_calls_used": 4,
    "tool_call_budget": 30
  },

  "risk_metadata": {
    "initial_score": 0.15,
    "flagged_by_feedback": false
  }
}
```

### 5.2 Layer 1: Synchronous Auditor Plugin Interface

Any custom audit plugin (in-process or webhook) adheres to this simple contract:

```python
from typing import Protocol, Literal
from dataclasses import dataclass

@dataclass
class AuditContext:
    trace_id: str
    session_id: str
    agent_id: str
    action_type: Literal["llm_call", "mcp_tool", "egress_http"]
    action_name: str
    payload: dict
    current_policy_level: str  # "standard", "strict", "quarantine"
    task_contract: dict | None = None  # Bound at session start (allowed_tools, budgets, postconditions)

@dataclass
class AuditDecision:
    decision: Literal["ALLOW", "BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT"]
    reason: str | None = None
    violation_code: str | None = None
    modified_payload: dict | None = None

class ActionAuditorPlugin(Protocol):
    """Interface for synchronous Layer 1 policy auditors."""
    
    def evaluate(self, ctx: AuditContext) -> AuditDecision:
        """Evaluates an agent action before it reaches upstream or executes."""
        ...
```

### 5.3 Layer 3: Consumer Plugin Interface

```python
from typing import Protocol
from dataclasses import dataclass

@dataclass
class ConsumerContext:
    consumer_name: str
    feedback_bus: "FeedbackControlChannel"

class ConsumerModule(Protocol):
    """Interface for asynchronous Layer 3 observers and risk graders."""
    
    def setup(self, ctx: ConsumerContext) -> None:
        """Initialize local state, thresholds, or models."""
        ...
        
    def process_event(self, event: dict, ctx: ConsumerContext) -> None:
        """
        Process persisted action event.
        Can emit metrics, store derived observations, or trigger dynamic feedback.
        """
        ...
        
    def teardown(self) -> None:
        """Flush state and cleanup resources."""
        ...
```

### 5.4 Gateway-Dashboard API Contract (for Judge UI)

To power the judge-facing audit ledger UI defined in `docs/dashboard-ui.md`, the Gateway exposes the following lightweight HTTP/SSE endpoints:

| Endpoint | Method | Purpose | Payload / Response |
|---|---|---|---|
| `/api/v1/inspect` | `POST` | **Attack Console**: Evaluates user prompts or tool calls synchronously against active rules. | **Req**: `{"prompt": "...", "strictness": "strict", "agent_id": "..."}`<br/>**Res**: `{"verdict": "BLOCKED"\|"REDACTED"\|"ALLOWED", "pipeline": [{"step": "01 auth", "status": "pass", "latency_ms": 0.2}, {"step": "02 PII", "status": "REDACT", "latency_ms": 0.6, "matches": ["PESEL"]}], "sanitized_prompt": "...", "model_response": "..."}` |
| `/api/v1/events/stream` | `GET` | **Live Feed**: Server-Sent Events (SSE) streaming decisions in real-time. | `data: {"event_id": "...", "ts": "...", "verdict": "BLOCKED", "rule": "R-SANCTIONS", "excerpt": "...", "latency_ms": 1.2}` |
| `/api/v1/policy` | `GET` | **Policy Inspector**: Returns current policy version, loaded rules, and strictness mode. | `{"version": "v12", "mode": "strict", "rules_count": 18, "last_reloaded": "..."}` |
| `/api/v1/policy/mode` | `POST` | **Strictness Toggle**: Dynamically switches active policy mode without restarting the gateway. | **Req**: `{"mode": "lenient"\|"standard"\|"strict"}`<br/>**Res**: `{"status": "ok", "active_mode": "strict", "version": "v13"}` |
| `/api/v1/suite/status` | `GET` | **Test Suite Strip**: Returns summary of the 28 automated test scenarios. | `{"total": 28, "passed": 28, "false_positives": 0, "categories": {"pii": "pass", "injection": "pass", "budget": "pass", "trajectory": "pass", "outcome": "pass"}}` |
| `/api/v1/scenario/replay` | `POST` | **Outcome Replay**: Replays recorded agent run (e.g. `TXM-03` alert close) and evaluates postconditions live. | **Req**: `{"scenario_id": "TXM-03"}`<br/>**Res**: `{"steps": [...], "outcome_verdict": "BLOCKED — outcome mismatch", "postcondition_failed": "TXM-P1", "evidence": "Structuring rule R-2 still fires on ground truth"}` |

---

## 6. Operability & Local Deployment Model

The architecture is designed to be set up on a developer's machine in minutes, without requiring cloud accounts or complex orchestrators.

### 6.1 Local Architecture Topology

```
+--------------------------------------------------------------------------+
| Single Local Host / Developer Machine                                   |
|                                                                          |
|  [Agent Process]  (Python / Node / AutoGen / LangGraph / Claude Code)    |
|        │                                                                 |
|        ▼ (localhost:8080)                                                |
|  +────────────────────────────────────────────────────────────────────+  |
|  | Local Control Gateway Process                                      |  |
|  |  ├── Interception Engine (FastAPI / ASGI / Go Daemon)               |  |
|  |  ├── In-Process Policy Registry & Dynamic Policy Cache             |  |
|  |  ├── Transparent LLM & MCP Provider Routing Table                 |  |
|  |  └── Queue 1 Ingestion Buffer (In-memory ring / SQLite / Redis)    |  |
|  +────────────────────────────────────────────────────────────────────+  |
|        │                                                                 |
|        ▼                                                                 |
|  +────────────────────────────────────────────────────────────────────+  |
|  | Local Persistence Worker                                            |  |
|  |  ├── Appends to Local MongoDB (localhost:27017)                    |  |
|  |  └── Feeds Queue 2 (with backoff & Dead-Letter Queue)              |  |
|  +────────────────────────────────────────────────────────────────────+  |
|        │                                                                 |
|        ▼                                                                 |
|  +────────────────────────────────────────────────────────────────────+  |
|  | Consumer & Risk Intelligence Daemon                                 |  |
|  |  ├── Metric Aggregators & Cost Sinks                               |  |
|  |  ├── Trajectory Anomaly & Loop Detectors                           |  |
|  |  └── Feedback Signal Bus ───[IPC Socket / Shared Memory]───► (Cache)|
|  +────────────────────────────────────────────────────────────────────+  |
|        │                                                                 |
|        ▼                                                                 |
|  [Local Web UI / Observability Dashboard] (localhost:3000)               |
+--------------------------------------------------------------------------+
```

### 6.2 Deployment Options

| Mode | Target Use Case | Components | Setup Simplicity |
|---|---|---|---|
| **All-in-One Local Daemon** | Quick local dev, unit & integration tests | Single Python/Go process running gateway, worker, and consumer threads + local MongoDB instance (or SQLite document JSON mode) | ⭐⭐⭐⭐⭐ (Zero overhead, single command) |
| **Docker Compose** | Reproducible team environments, Hackathon demos | Containers: `gateway`, `persistence-worker`, `mongo:latest`, `consumer-runner`, `dashboard` | ⭐⭐⭐⭐ (Isolated, single `docker compose up`) |
| **Sidecar Container** | Containerized agent deployment | Gateway runs in the same pod/network namespace as the agent container, binding to `localhost` | ⭐⭐⭐⭐ (Production-ready local isolation) |

---

## 7. Extensibility & Development Guide

### How to Add a New Auditor Plugin to Layer 1
1. **Implement the contract:** Write a class fulfilling `ActionAuditorPlugin` (or expose a simple HTTP POST endpoint returning JSON).
2. **Register in configuration:** Add the plugin entry to `gateway_config.yaml` under `interception.auditors`.
3. **Specify behavior on violation:** Choose whether to `BLOCK` (drop action and return error) or `REDACT` (cleanse arguments).

### How to Add a New Risk Grader with Feedback to Layer 3
1. **Implement `process_event`:** Track relevant historical actions for the session in memory or local cache.
2. **Calculate risk heuristic:** Check for anomalous behavior (e.g., unexpected arguments, forbidden transitions).
3. **Emit feedback signal:** Call `ctx.feedback_bus.emit_adjustment(PolicyAdjustmentSignal(...))` to dynamically tighten the gateway's rules.

---

## 8. Summary Comparison: Enterprise Spec vs. Local Gateway

| Dimension | Previous Enterprise Spec | Refocused Local Gateway Architecture |
|---|---|---|
| **Deployment Target** | Distributed cloud cluster (K8s, Kafka, S3, ClickHouse) | **Locally run gateway/proxy** on the same host as the agentic loop |
| **Interception Role** | Passive observation (post-hoc monitoring, streaming tee) | **Active Control**: Synchronous auditing, in-process/webhook policy guards, action blocking |
| **Backend Routing** | Simple passthrough | **Transparent routing & provider fallback** (LLM & MCP failover) |
| **Persistence** | Multi-tier (WAL -> Broker -> S3 Parquet -> ClickHouse) | **Document Store (MongoDB)** for rapid append-only JSON storage |
| **Consumer Execution** | Distributed consumer groups on Kafka | **Supervised Local Queue 2** with exponential backoff & DLQ |
| **Observability -> Control** | One-way pipeline (observe only) | **Closed-loop dynamic feedback**: consumers dynamically update Layer 1 policy cache |
