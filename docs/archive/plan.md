## AI Control Layer — Project Direction

We want to build a **runtime control and verification layer for autonomous AI agents**. The key idea is to move beyond isolated, rule-based checks such as blocking a dangerous shell command or detecting a leaked API key. Those controls are necessary, but they do not address the harder problem: whether an agent is behaving correctly across an entire multi-step workflow and whether it actually achieves its intended goal.

The challenge explicitly asks for a control layer that can intercept agent interactions, enforce centralized policies, combine deterministic and semantic controls, govern resources, and provide security reporting and testing. Our approach extends this into **trajectory-level supervision**.

For every agent execution, the system maintains an external representation of the task: the declared role of the agent, the original objective, relevant constraints, actions already taken, tool outputs, current state, and expected final outcome. Instead of judging each tool call independently, the control layer evaluates whether the current trajectory remains consistent with the task and whether the final state actually satisfies the objective.

The system therefore operates at three levels:

1. **Action-level security.** Deterministic controls enforce permissions, information-flow rules, authentication, PII/secret handling, attack signatures, budgets and other hard constraints. OpenAPPA can serve as the information-flow and policy-enforcement component here.

2. **Trajectory-level semantic supervision.** The system continuously analyzes the observable execution history — prompts, tool calls, results and state changes — and determines whether the agent's current behavior is still consistent with its assigned role and goal. This catches failures that cannot be detected by inspecting a single call.

3. **Outcome verification.** The system independently verifies whether the agent actually accomplished the requested task. Agent self-reporting is not treated as proof of success. Where possible, the resulting external state is checked against explicit invariants, expected values and postconditions.

### Example 1 — Trade booking

Consider an agent whose only responsibility is to book already-approved trades.

A system with an 80% successful booking rate is not production-ready, even if every individual API call is technically authorized. The relevant question is not only whether the agent was allowed to call `book_trade()`, but whether the complete workflow produced the exact expected booking.

The control layer can track:

```text
Approved instruction
→ correct account
→ correct instrument
→ correct quantity
→ booking submitted
→ booking persisted
→ resulting booking matches instruction
→ no duplicate or unintended side effect
```

If the agent reports success but the resulting booking differs from the approved instruction, the execution is marked as failed or escalated even though no traditional security rule was violated.

### Example 2 — Payment processing

A payment agent receives an instruction to pay invoice #481 for €12,500.

Each individual operation may look legitimate:

```text
read_invoice()
lookup_supplier()
get_bank_account()
create_payment()
```

However, trajectory-level supervision can detect that the payment account suddenly differs from the account associated with the approved invoice.

The final verifier can enforce:

```text
payment.amount == approved_invoice.amount
payment.recipient == approved_supplier
payment.currency == approved_invoice.currency
payment.executed_once == true
```

The system therefore detects both malicious behavior and ordinary agent errors.

### Example 3 — Customer support agent

A support agent is asked to refund one order.

During execution, it may legitimately access the order, customer record and payment information. A basic permission system may allow all of those operations.

If the agent starts querying unrelated customer accounts or attempting actions unrelated to the refund, the trajectory can be flagged as inconsistent with the original objective before a clearly malicious individual operation appears.

The system can then choose between continuing, logging, warning, requiring human approval, constraining the agent, or blocking the action depending on confidence and potential impact.

## Architecture

The resulting architecture is approximately:

```text
Agent / Application
        │
        ▼
AI Control Layer
        │
        ├── deterministic controls
        │     ├── authentication / authorization
        │     ├── information-flow policy
        │     ├── PII / secrets
        │     ├── signatures / known attacks
        │     └── resource / budget limits
        │
        ├── trajectory engine
        │     ├── declared role
        │     ├── original objective
        │     ├── execution state
        │     ├── semantic goal alignment
        │     └── anomaly / deviation detection
        │
        ├── outcome verifier
        │     ├── invariants
        │     ├── postconditions
        │     └── external-state validation
        │
        └── audit / telemetry / dashboard
                │
                ▼
        LLM / MCP / API / Agent / Tool
```

OpenAPPA gives us a strong starting point for deterministic information-flow enforcement, policies, sanitization, approvals and policy testing. The challenge explicitly allows building on open-source components.

Our main contribution would therefore be the broader runtime around it: **continuous semantic supervision of agent trajectories and independent verification that autonomous workflows actually reach their intended state correctly and safely.**

The core thesis is:

> **Do not only ask whether an agent is allowed to perform the next action. Verify that the sequence of actions still serves the intended goal, and that the final real-world outcome is actually correct.**