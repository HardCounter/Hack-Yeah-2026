#!/usr/bin/env bash
# Terminal 1: start the complete governed pipeline (Layer 1 gateway -> Layer 2 evidence -> Layer 3
# consumers) on a fresh synthetic bank, traced to a file, ready for an interactive OpenCode session.
#
# Usage: scripts/run_live_pipeline.sh [APP-0001] [--model provider/model] [--port 8080] [--bank-db path]
#   Terminal 2: scripts/run_opencode_intercepted.sh   then in OpenCode: /intercept-run Process application APP-0001
#   Terminal 3: tail -f var/live-runs/<run>/control-layer.log
#   Ctrl+C in terminal 1 finishes the session, verifies the outcome and prints the verdict.
# Uses your normal OpenCode provider login; the model defaults to the one in the repo opencode.json.
set -euo pipefail

cd "$(dirname "$0")/.."
unset VIRTUAL_ENV
command -v uv >/dev/null 2>&1 || { echo "error: uv is not installed" >&2; exit 1; }
command -v opencode >/dev/null 2>&1 || echo "warning: opencode is not on PATH; terminal 2 will need it" >&2
exec uv run --locked python -m simulation.live_pipeline "$@"
