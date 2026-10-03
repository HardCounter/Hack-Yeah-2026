#!/usr/bin/env bash
# Run the whole control layer once, offline: scripted KYC agent -> governed gateway (Layer 1)
# -> durable evidence (Layer 2) -> consume-plane plugins (Layer 3) -> independent verification.
# No model and no network: the agent's steps are scripted; faults inject known mistakes.
#
# Usage: scripts/run_demo.sh [APP-ID] [--fault FAULT ...]
#   scripts/run_demo.sh                                            # APP-0001, clean -> VERIFIED_SUCCESS
#   scripts/run_demo.sh APP-0003 --fault skip_step:screen_sanctions # -> VERIFICATION_INCOMPLETE (exit 1)
#   scripts/run_demo.sh APP-0011 --fault repeat:create_client
# Each run uses an isolated copy of data/bank.db (generated on first use; seed 2026).
set -euo pipefail

cd "$(dirname "$0")/.."
unset VIRTUAL_ENV
command -v uv >/dev/null 2>&1 || { echo "error: uv is not installed" >&2; exit 1; }

app=APP-0001
if [[ $# -gt 0 && "$1" != -* ]]; then app=$1; shift; fi
if [[ ! -f data/bank.db ]]; then
    echo "== generating the synthetic bank (data/bank.db)"
    uv run --locked python data/generate.py >/dev/null
fi
echo "== $app (scripted driver) $*"
exec uv run --locked python -m simulation.agent "$app" --driver scripted "$@"
