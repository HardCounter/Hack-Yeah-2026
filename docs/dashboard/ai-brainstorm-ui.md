# UI idea: brainstorm plus additions

Our current idea for the UI, based on [dashboard-brainstorm.md](dashboard-brainstorm.md), with the
gaps against the Goldman Sachs brief filled in. Items marked **(added)** were not in the handwritten
brainstorm.

Status: idea, not implemented.

---

## Hosting and access

- The app is **deployed and publicly accessible** (Docker, for example on AWS ECS). A hosted website
  connected to the container with the app is a must-have for judging.
- **As frictionless as possible for the judge**: one URL, no install, no login, no API keys. The judge
  opens the link and can use every view straight away.
- Flow: `agent → proxy → dashboard`.
- **(added)** A status line visible on every view: gateway up, LLM API reachable, policy version
  loaded. If something is down the judge sees it instead of a silent failure.

---

## The four views

### 1. Main dashboard (whole-system view)

From the brainstorm:
- One big dashboard that traces everything: metrics plus links to the concrete conversations,
  especially the flagged ones.
- A `risk × impact` metric.
- Everything the rules and guidelines say should be observable.
- A button to clear the stored data.

Added:
- **(added) Budget and cost**: token, cost and compute spend against the limit, per session and per
  agent, for the paid API models we call.
- **(added) Performance telemetry**: latency per control and gateway overhead (p50 / p95), split into
  deterministic and semantic controls.
- **(added) Audit log export**: download the audit log as JSONL or CSV.
- **(added) Security posture summary**: decisions by verdict, top rules triggered, blocked-threat count.

### 2. Configuration page

From the brainstorm:
- A page to read and change the configuration.

Added:
- **(added) Changes apply without a restart.** After saving, the page shows the new policy version and
  that it is in force.
- **(added) Validation** with readable, line-level errors before a change is applied.
- **(added) Strictness selector**: lenient / standard / strict.
- **(added) Revert to default**, always visible, so a judge's experiment can be undone in one click.
- **(added) Attack signature feed**: a list of signatures for known AI exploits (malicious code
  execution, unsafe deserialization, model-repo supply chain) with add and remove. This is separate
  from the policy file.

### 3. Chat console

From the brainstorm:
- Send commands to the agent and any other prompts.
- Tracing for the prompt just sent: a mini dashboard for a single conversation.

Added:
- **(added) Original vs redacted text**: what the judge typed next to what the model actually saw, so a
  redaction is visible and not only a label.
- **(added) Per-control pipeline trace**: each control in order with its result and latency.
- **(added) Approve / Reject buttons** when a request needs human approval. Nothing executes until the
  judge clicks.
- **(added) Presets** ("Try this:") so nobody faces an empty box, including one benign request that
  must pass.

### 4. Test view

From the brainstorm:
- Which tools failed and how many tests there are in total, by category.
- A button to run all tests and buttons to run specific groups.

Added:
- **(added) Positive and negative cases reported separately**: "blocked / redacted as expected: X/Y"
  and "allowed as expected: X/Y, false positives: N".
- **(added) Expected vs actual per case**, opened by clicking the case.
- **(added) Budget and exploit-mitigation cases** shown as their own groups, since the brief names them.

---

## Two test tracks

1. **Without AI**: many automated tool calls and other mocked use cases of our guardrail. Fast and
   repeatable.
2. **Agent track**: a real AI agent calls the tools.

**(added)** Results from the agent track are labelled as such, because they vary by model. Anything
recorded or mocked is labelled too; we never show recorded numbers as live.

---

## Tracing and verdicts

- We collect data about the agent's work: tracing, for example the prompt history, and flag the
  problematic entries.
- Each entry is color coded by verdict: `ALLOW`, `BLOCK`, `REDACT`, `APPROVE`, `ALERT`.
- **(added) Outcome check ("agent said done, was it?")**: after an agent run, an independent check of
  the persisted result, shown on the trace. This catches runs where every step looked fine and the
  result was still wrong.

---

## Metrics (first set)

The first set of metrics for the main dashboard. More will be added incrementally as the backend
layers get connected.

### Available from the gateway today

Source: the gateway's audit events (`intercept/server.py`), one per evaluated tool call.

| # | Metric | Definition | Source field |
|---|---|---|---|
| 1 | Allowed vs blocked | Count and share of final decisions | `decision` (`ALLOW` or `BLOCK`) |
| 2 | Blocks by reason | Blocked calls grouped by reason | `code`: `TOOL_DENIED`, `TASK_SCOPE`, `BUDGET_EXHAUSTED`, `REPLAY`, `UNKNOWN_RUN`, `AUDITOR_BLOCK`, `APPROVAL_NOT_IMPLEMENTED` |
| 3 | Hits per control | For each control: how often it returned ALLOW / BLOCK / REDACT / ALERT | `auditor_decisions[].auditor`, `.decision`, `.code` |
| 4 | Redactions and signature hits | Count of redactions and of signature matches, per control | `auditor_decisions[]` with `decision = REDACT` or `code = SIGNATURE_MATCH` |
| 5 | Latency per control | p50 / p95 time each control takes | `auditor_decisions[].latency_ms` |
| 6 | Tool calls used vs budget | Calls used against the limit, per session | `tool_calls_used` + `tool_call_budget` in the policy |
| 7 | Tool results | Completed vs error | observation events, `status` |
| 8 | Usage by tool and contract | Calls per tool; sessions per contract | `tool`, `run_bound` events |
| 9 | Active policy version | Version (config hash) currently in force | `policy_version` |

### Needs backend work first

The gateway does not see model calls yet and the agent does not record token counts, so these have
no data today. They are in the first set because the brief requires budget governance.

| # | Metric | Definition | What is missing |
|---|---|---|---|
| 10 | Tokens | Input + output tokens against the limit, per session and per agent | Token counts recorded for each model call, and a token budget in the policy |
| 11 | Cost | Estimated cost against the limit | A price table per model; cost derived from tokens |
| 12 | Compute time | Seconds of model time per call, against a time limit | Model-call duration recorded, and a compute-time budget in the policy |

---

## Open points

- **Clear-data button**: on a shared hosted deployment one judge could erase another judge's traces,
  and wiping an audit log looks odd in a security product. Decide whether it resets only the judge's
  own session.
- **Config edits on a shared deployment**: decide whether one judge's change affects everyone or only
  their session.
- **`risk × impact`**: needs a definition (for example a fixed severity per rule).
