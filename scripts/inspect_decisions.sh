#!/usr/bin/env bash
# Run governed scenarios and print each session's control-plane decision trace (docs/decision-trace.md).
#
# Usage: scripts/inspect_decisions.sh [SCENARIO ...] [--json] [--serve] [--port 8790] [--judge]
#   SCENARIO: clean | skip-screening | duplicate-create | out-of-scope (default: all)
#   --serve keeps the read-only REST API running on 127.0.0.1 so the trace can be browsed.
set -euo pipefail

cd "$(dirname "$0")/.."
unset VIRTUAL_ENV
command -v uv >/dev/null 2>&1 || { echo "error: uv is not installed" >&2; exit 1; }
exec uv run --locked python -m persistence.http_api.decisions_demo "$@"
