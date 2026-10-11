---
description: Check independent outcome verification and negative-test coverage.
argument-hint: [scope]
---
Use the verification-auditor subagent (Agent tool) for this task.


Review outcome verification and test adequacy. Scope: $ARGUMENTS
If no scope is given, inspect current contracts, verifiers, state adapters, and tests. Check trusted approved state, independently queried persisted outcomes, duplicates, false success, retries, and unavailable state. Return prioritized findings and a concrete positive/negative test matrix. Do not edit files or claim tests were executed.
