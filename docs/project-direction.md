# AI Control Layer: Project Direction

## Core Thesis

Authorization answers whether an agent may perform an action. Our control layer must also answer whether the action serves the assigned task and whether the completed workflow produced the intended external result.

Build a policy-driven runtime control and verification layer between autonomous agents and LLMs, MCP servers, APIs, tools, and other agents. Its differentiator is a unified external Task Contract, supervision of the observable execution trajectory, and independent state-based outcome verification. It is not merely per-call filtering, and it does not require private chain-of-thought.

This document records the proposed direction supplied for agent setup and is pending team confirmation. It is not a claim that the features below have already been implemented. The competition requirements remain in `GoldmanSachsRules.md` and `GoldmanSachsCriteria.md`.

## Task Contract

Create a contract independently of the governed agent for each execution. It describes:

- Run identity, declared agent role, and original objective.
- Trusted approved instruction and relevant initial external state, with provenance.
- Permitted resources, models, tools, actions, and expected side effects.
- Hard constraints, resource/token/API budgets, and approval requirements.
- Explicit outcome invariants and postconditions, including uniqueness where needed.
- Applicable policy identity/version and verification strategy.

An agent may propose an action, but cannot redefine its own authority, approved economic terms, or success criteria. Changes to trusted instructions or approval state must come from an authorized external actor and remain auditable.

## Three Complementary Levels

### 1. Deterministic Runtime Enforcement

Evaluate cheap, explainable hard checks before semantic assessment and before forwarding an interaction. Inspect results before delivering them to an agent when output controls apply.

Controls include authentication/authorization, task-scoped resource access, data-flow restrictions, PII and secrets, known attack signatures, permitted tools/models, quotas, budgets, and required approvals. Support decisions such as ALLOW, WARN, REDACT, APPROVE, and BLOCK. An APPROVE decision pauses the action until valid approval is obtained; it is not immediate permission to execute.

### 2. Trajectory-Level Semantic Supervision

Track the original objective, messages, tool calls and arguments, results, information sources, side effects, resource usage, and relevant state. Selectively assess consistency with the task and role, especially before high-impact or irreversible operations.

Detect goal drift, suspicious detours, unjustified resource access, substitutions of approved terms, and attempted repeated actions. Do not add an LLM assessment to every interaction by default. Semantic assessments provide contextual evidence and risk decisions, not authority to bypass deterministic constraints.

### 3. Independent Outcome Verification

Query the resulting external system through a trusted verification path and check explicit invariants. An agent declaring success does not complete verification. Prefer deterministic comparisons and counts; use semantic verification only for outcomes that cannot be structurally represented, with that limitation disclosed.

Distinguish verified success, failed postconditions, and verification that could not be completed. If an incorrect side effect already happened, verification detects it; do not claim that detection itself prevented or rolled back the action.

## Example Workflows

### Trade Booking

Book an already-approved instruction without changing its economic terms:

```text
approved instruction -> retrieve -> resolve booking information
                     -> submit -> persist -> independently verify
```

Check the persisted booking against the approved `trade_id`, `account`, `instrument`, `side`, `quantity`, and `currency`. Require `booking_count(approved.trade_id) == 1`. Supervise account changes, unrelated-trade lookups, and attempted second bookings.

### Payment Processing

Pay approved invoice #481 for EUR 12,500. Reading an invoice, looking up a supplier, retrieving payment details, and creating a payment can all be individually authorized while still producing the wrong result.

Before creating the payment, compare proposed terms to trusted approved invoice and beneficiary state, not just an untrusted attachment or compromised supplier record. Independently verify invoice identity, amount, approved recipient and payment destination, currency, and exactly-once execution. Recipient identity alone does not establish that the destination bank account is correct.

### Customer Support

Refund one assigned order. Global access to customer/order/payment systems does not justify unrelated customer retrievals, unrelated orders, or additional refunds. Independently verify order identity, refund amount, original payment source, and expected refund count against the approved task.

## Runtime Architecture

```text
Central policy + external Task Contract
                  |
Agent/application -> Control Layer -> LLM / MCP / API / tool / agent
                       |
                       +-> deterministic enforcement
                       +-> selective trajectory supervision
                       +-> independent external-state verification
                       +-> sanitized audit / telemetry / dashboard

Threat/signature feeds -> deterministic enforcement
```

One centralized policy controls tools/models, data flow, budgets, semantic thresholds, approvals, expected side effects, and outcome invariants. Changes must be reloadable and testable without editing governed-agent code. Define how in-flight runs bind to policy versions; do not silently change the contract halfway through execution.

Reuse open-source infrastructure for interception, authentication, information-flow controls, accounting, and observability where useful. The primary contribution is the contract/supervision/verification layer above these primitives. Existing multi-step guardrail projects mean "looking at several tool calls" is not sufficient evidence of novelty.

## Evidence and Testing

Each run should produce a sanitized trace of objective, policy version, interactions, costs, decisions, semantic assessments, approvals/interventions, side effects, and final verification. Provide management metrics and drill-down security traces/exportable audit evidence.

Exercise the same policies and enforcement path through executable tests, including:

- Allowed workflows and legitimate task-scoped resource access.
- Unauthorized resources/tools/models, PII/secret leakage, and known exploit signatures.
- Prompt injection in untrusted sources and goal drift across individually allowed calls.
- Budget exhaustion, invalid/missing approval, and policy/threshold/feed changes.
- Incorrect persisted side effects, duplicate actions, and false success reports.
- Semantic service failure, verifier failure, and relevant concurrency/retry boundaries.

Deliver a functional intermediary, documented sample policy, architecture diagram, simple interactive dashboard, and judge-runnable tests. Keep a reproducible demo and setup within the team's available resources. Mocks are useful test fixtures but must not be represented as live semantic inference or independent verification of a real external system.
