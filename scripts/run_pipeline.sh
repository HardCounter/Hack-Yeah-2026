#!/usr/bin/env bash
# Run one synthetic KYC application through the whole local pipeline:
# isolated bank -> governed gateway -> OpenCode agent -> independent verification.
#
#   scripts/setup_opencode_pipeline.sh
#   scripts/run_pipeline.sh APP-0001 --model provider/model
#   scripts/run_pipeline.sh --help
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
command -v uv >/dev/null 2>&1 || { echo "error: uv is required; run scripts/setup_opencode_pipeline.sh first" >&2; exit 1; }

if [[ "${1:-}" != "--help" && "${1:-}" != "-h" && -z "${OPENCODE_BIN:-}" ]]; then
    local_bin="$ROOT/var/opencode-cli/node_modules/.bin/opencode"
    # The runner accepts the pinned CLI or any OpenCode 2.x on PATH; install only when neither exists.
    if [[ ! -x "$local_bin" ]] && ! command -v opencode >/dev/null 2>&1; then
        echo "OpenCode CLI is missing; running scripts/setup_opencode_pipeline.sh" >&2
        "$ROOT/scripts/setup_opencode_pipeline.sh"
    fi
fi

unset VIRTUAL_ENV
cd "$ROOT"
exec uv run --locked python -m simulation.opencode_runner "$@"
