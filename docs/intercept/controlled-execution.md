# Controlled Python tool execution milestone

The tool registry moved to `simulation/tools/` on main. The gateway now offers
`POST /v1/tools/execute` as an **opt-in** path through mandatory hard checks,
configured auditors, final-payload checks, admission evidence, and Python dispatch.
It uses the same authenticated action envelope as `/v1/actions/evaluate`:

```json
{"session_id":"synthetic-session","call_id":"call-1","tool":"request_more_docs","arguments":{"app_id":"APP-0001","reason":"Synthetic demo"}}
```

Enable only against a disposable synthetic bank database:

```sh
uv run --locked python -m intercept.service.server --policy config/intercept.demo.yaml --audit /tmp/opencode/intercept-demo.jsonl --trace --bank-db /path/to/disposable/bank.db
uv run --locked pytest tests/support/test_policy.py tests/support/test_server.py tests/support/test_auditors.py tests/support/test_execution.py -v
```

The sample policy must be adjusted by the trusted operator to allow and scope
`request_more_docs` before the example executes. Model-supplied identity/database
paths are rejected by the envelope; `--tool-agent` and `--bank-db` are operator
settings. Without `--bank-db`, dispatch returns `TOOL_EXECUTION_DISABLED`.

## Execution boundary

`intercept.tools.execution.ToolExecutor` starts an isolated Python process per call.
It invokes `intercept.tools.worker`, importing the existing simulation registry
without changing coworker-owned tools. This avoids colliding with other Python
modules named `registry`. It is **not** an OS security sandbox.

No tool executes after an initial/final policy denial or before admission evidence
has been accepted into the ingestion queue. SQLite calls run off the event loop.
The registry independently validates arguments and its configured simulation
identity. Returned tool data is sent to the caller, never the intercept audit log.
That registry's own `audit_actions` table can retain synthetic arguments/results;
it is a simulation execution trace, not the sanitized intercept reporting store.

## Validation and limitations

The complete interception Python suite has **25 passing tests**. Four new tests
read actual persisted SQLite state independently of the gateway response:

- Allowed status write persists; concurrent same-call replay executes once.
- Wrong application is denied with no execution audit row; known audit failure
  also prevents the side effect.
- A tool reports successful client creation without prior sanctions screening;
  independent `data.postconditions.verify_onboarding` detects ONB-P1 failure.
- Execution without the operator's explicit opt-in never reaches the registry.

Initial test failures were corrected: the generated declaration field is
`date_of_birth`; importing a TestCase class directly also caused duplicate test
discovery. The reported count is the rerun's **distinct** tests.

This is a real synthetic gateway/SQLite integration, **not** a live OpenCode run.
OpenCode's current adapter still uses admission-only hooks; routing OpenCode
custom tools through this dispatch endpoint remains the next integration task.
The dispatcher does not yet enforce workflow-specific screening prerequisites;
the negative verification test deliberately demonstrates false business success.

Call-id replay protection is in memory and does not deduplicate new call IDs by
business target. Restart recovery and exactly-once business writes remain open.
Do not retry after a timeout or ambiguous execution result: a subprocess may have
committed before it was terminated or the HTTP response failed. A post-execution
audit failure cannot undo the write. Results remain `NOT_VERIFIED` until a trusted
external-state verifier explicitly checks the workflow.

Full output filtering, approvals, model/MCP proxy paths, durable accounting,
consumer-plane delivery, and a terminal OpenCode demo remain incomplete. See
[remaining roadmap](configurable-pipeline.md) rather than assuming those paths
are covered by this dispatch endpoint.
