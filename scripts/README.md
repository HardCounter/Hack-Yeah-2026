# Scripts

Run from anywhere; each script `cd`s to the repository root. Requirements: `uv`; `node` 22+ for
adapter tests; `opencode` 2.x for anything that starts OpenCode. Synthetic data only.

## Run the control layer

| Script | What it does |
|---|---|
| `run_live_pipeline.sh [APP-0001] [--model p/m] [--port 8080]` | **Full pipeline for an interactive OpenCode session.** Starts gateway → evidence store → consumers on a fresh synthetic bank, traces every step to `var/live-runs/<run>/control-layer.log`, and waits. Ctrl+C finishes the session, verifies the outcome and saves artifacts. |
| `run_opencode_intercepted.sh [opencode flags]` | Terminal 2: starts `opencode --standalone` wired to whichever service terminal 1 started (`var/intercept.env`). |
| `run_demo.sh [APP-ID] [--fault F]` | Whole control layer offline with a scripted agent (no model, no network); prints the trace and the verdict. |
| `run_pipeline.sh APP-0001 --model p/m` | Non-interactive: OpenCode works one application through the governed tools, then verification; artifacts in `var/pipeline-runs/`. |
| `setup_opencode_pipeline.sh` | Installs the locked Python env and a pinned OpenCode 2.0.22 under `var/opencode-cli` (only needed without `opencode` on PATH). |
| `run_intercept_receiver.sh` | Observe-only receiver: logs every adapter request and ALLOWS it (diagnostics, no enforcement). |
| `run_consume_plane.sh [events.jsonl]` | Replays a recorded run through the consume plane and prints the findings. |

### Trace a live OpenCode run

```bash
scripts/run_live_pipeline.sh APP-0001                 # terminal 1 (prints the trace path)
scripts/run_opencode_intercepted.sh                   # terminal 2, then in OpenCode:
                                                      #   /intercept-run Process application APP-0001
tail -f var/live-runs/APP-0001-*/control-layer.log    # terminal 3: one JSON line per step
```

Expected trace for each tool call:
1. `intercept action.received`
2. `persistence evidence.committed` (with `seq`)
3. `intercept action.decided` (`ALLOW`/`BLOCK` + reason)
4. `consume event.processed` (and `finding` / `feedback.proposed` when a plugin reacts)

`intercept adapter.connected` appears when OpenCode loads the plugin. After Ctrl+C,
`consume verification` and `intercept session.verified` carry the verdict.

## Test

| Script | What it does |
|---|---|
| `test.sh [all\|python\|intercept\|persistence\|consume\|e2e\|adapter\|data] [pytest args]` | All tests (default) or one layer's. |
| `test_consume_plane.sh` | Consume-plane tests only. |
| `test_opencode_adapter.sh [--quiet]` | OpenCode adapter Node tests; prints every JSON request the plugin sends. |
| `check_opencode_pipeline.sh` | Starts real OpenCode and passes when the adapter's handshake reaches Python (no prompt sent). |

Tracing in any script or command: `CONTROL_LOG=terminal|file|null`, `CONTROL_LOG_FILE=path`.
