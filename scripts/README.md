# Scripts

Run from anywhere; each script `cd`s to the repository root. Requirements: `uv`; `node` 22+ for
adapter tests; `opencode` 2.x for anything that starts OpenCode. Synthetic data only.

## Run the control layer

| Script | What it does |
|---|---|
| `run_opencode_intercepted.sh [--workspace DIR] [opencode flags]` | Terminal 2: starts `opencode --standalone` wired to whichever service terminal 1 started (`var/intercept.env`). Defaults to the run's temporary project; `--workspace` selects another existing project directory while retaining the run-specific adapter/agent config. |
| `run_live_pipeline.sh [APP-0001] [--model p/m] [--port 8080] [--api-port 8790] [--cors-origin URL] [--bank-db PATH] [--runs-dir DIR] [--no-api]` | **Full pipeline for an interactive OpenCode session.** Starts gateway → evidence store → consumers on a fresh synthetic bank, plus the read-only REST API for dashboards. Traces every step to `var/live-runs/<run>/control-layer.log` and waits. Ctrl+C finishes the session, verifies the outcome, saves artifacts and stops both processes. |
| `run_rest_api.sh [--port 8790] [--cors-origin URL] [--evidence-dir DIR] [--example-mode]` | Read-only SQLite evidence API ([docs/rest.md](../docs/rest.md)). Deferred queries return 501; explicit `--example-mode` serves frontend fixtures. Binds loopback. Docs UI at `/api/v1/docs`. |
| `run_rest_demo.sh [--port 8790] [--runs-dir var/rest-demos]` | Generates two actual governed synthetic sessions and independent verification, then serves their real SQLite evidence. No OpenCode/model/provider needed; prints IDs and curl commands. |
| `run_demo.sh [APP-ID] [--fault F]` | Whole control layer offline with a scripted agent (no model, no network); prints the trace and the verdict. |
| `run_pipeline.sh APP-0001 --model p/m [options]` | Non-interactive: OpenCode works one application through the governed tools, then verification; artifacts in `var/pipeline-runs/`. Accepts `--bank-db`, `--runs-dir`, `--policy`, `--timeout`, `--prompt`, `--free`. |
| `setup_opencode_pipeline.sh` | Installs the locked Python env and a pinned OpenCode 2.0.22 under `var/opencode-cli` (only needed without `opencode` on PATH). |
| `run_intercept_receiver.sh` | Observe-only receiver: logs every adapter request and ALLOWS it (diagnostics, no enforcement). Supports `PORT=8080`, `PROMPTS=observe`, `COMPACT=1`. |
| `run_consume_plane.sh [events.jsonl [contracts.jsonl]]` | Replays a recorded run through the consume plane and prints findings. Supports `CONFIG=consume_plane.yaml`, `LEDGER=:memory:`, `LOG_LEVEL=INFO`. |
| `inspect_decisions.sh [SCENARIO ...] [--json] [--serve] [--judge] [--port PORT] [--runs-dir DIR]` | Runs governed scenarios (clean, skip-screening, duplicate-create, out-of-scope) and prints each session's control-plane decision trace; `--serve` keeps the REST API up to browse it. See [decision trace](../docs/decision-trace.md). |

### Trace a live OpenCode run

```bash
scripts/run_live_pipeline.sh APP-0001                       # terminal 1 (prints the trace path)
scripts/run_opencode_intercepted.sh --workspace "some_path" # terminal 2, interceped opencode in workspace

# or, in terminal 2, choose another existing project directory:
scripts/run_opencode_intercepted.sh --workspace "$PWD/team-data"
tail -f var/live-runs/APP-0001-*/control-layer.log    # terminal 3: one JSON line per step
```

`--workspace` changes OpenCode's project root only. Workspace-local OpenCode config is ignored so the run-specific adapter/agent config remains active; local filesystem and shell tools remain disabled for the governed agent.

Expected trace for each tool call:
1. `intercept action.received`
2. `persistence evidence.committed` (with `seq`)
3. `intercept action.decided` (`ALLOW`/`BLOCK` + reason)
4. `consume event.processed` (and `finding` / `feedback.proposed` when a plugin reacts)

`intercept adapter.connected` appears when OpenCode loads the plugin; its `directory` shows where
OpenCode opened. Only one pipeline or receiver can own `var/intercept.env` at a time: a second one
refuses to start while the first is running. After Ctrl+C,
`consume verification` and `intercept session.verified` carry the verdict.

### What `run_live_pipeline.sh` starts

| Process | Planes | Created | Log |
|---|---|---|---|
| `intercept.service.local` (port 8080) | Layer 1 gateway | At start | `gateway.log` |
| (same process, `GovernedRuntime`) | Layer 2 evidence store `bank-runs/<session_id>.evidence.db` and Layer 3 consume plane (trajectory-risk, outcome verifier, feedback to Layer 1) | When `/intercept-run` binds the session | `control-layer.log` |
| `persistence.http_api` (port 8790) | Read-only REST API over `bank-runs/` | At start | `read-api.log` |

The pipeline reports ready only after the gateway answers and the API serves a data read. The
gateway's connection details go to the private (0600) `var/intercept.env`, which
`run_opencode_intercepted.sh` (the agent's terminal) reads. Each run removes only its own file.
The REST API has **no authentication**. It listens on `127.0.0.1` only, and its process does not
get the gateway tokens.

```bash
curl http://127.0.0.1:8790/api/v1/sessions
curl http://127.0.0.1:8790/api/v1/system/stats   # evidence_stores: real count
```

If the API process dies, the control layer keeps enforcing; the pipeline prints a warning and
only the dashboard view is lost. Health, session list/detail/verification, action drill-down,
session trajectory and audit export now read actual SQLite state. Deferred metrics/detections
return 501 unless explicitly started with `--example-mode`.

## Test

| Script | What it does |
|---|---|
| `test.sh [all\|python\|intercept\|persistence\|consume\|e2e\|adapter\|data\|decisions] [pytest args]` | All tests (default) or one layer's. |
| `test_consume_plane.sh` | Consume-plane tests only. |
| `test_opencode_adapter.sh [--quiet]` | OpenCode adapter Node tests; prints every JSON request the plugin sends. |
| `check_opencode_pipeline.sh` | Starts real OpenCode and passes when the adapter's handshake reaches Python (no prompt sent). |

Tracing in any script or command: `CONTROL_LOG=terminal|file|null`, `CONTROL_LOG_FILE=path`.
