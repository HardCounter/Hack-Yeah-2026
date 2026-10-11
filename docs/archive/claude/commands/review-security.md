---
description: Review runtime security boundaries and concrete bypass risks.
argument-hint: [scope]
---
Use the security-auditor subagent (Agent tool) for this task.


Perform a read-only security review. Scope: $ARGUMENTS
If no scope is given, inspect the current control-layer implementation and policies. Return prioritized concrete findings with evidence, failure/attack sequences, fixes, and regression-test suggestions. Separate confirmed behavior from hypotheses and missing evidence. Do not attack live systems or edit files.
