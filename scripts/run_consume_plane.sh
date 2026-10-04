#!/usr/bin/env bash
# Run the consume plane (Layer 3) on a recorded run and show what the plugins produced.
#
# Usage: scripts/run_consume_plane.sh [events.jsonl [contracts.jsonl]]
#   With no arguments it replays the bundled demo: 10 tool calls in 10 s, which trips the
#   consumer handlers from consume_plane.yaml (plugins/ contains Layer 1 auditors).
#   A sibling <name>.contracts.jsonl is used automatically when no contracts file is given.
#   Risk demo: scripts/run_consume_plane.sh tests/support/consume_plane/fixtures/risky_onboarding.jsonl
#
# Environment:
#   CONFIG=path       config file (default: consume_plane.yaml)
#   LEDGER=path       completion ledger (default: :memory:, so every run reprocesses all events)
#   LOG_LEVEL=level   default: INFO
set -euo pipefail

cd "$(dirname "$0")/.."
unset VIRTUAL_ENV  # use the project's .venv managed by uv

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is not installed (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

DEMO_EVENTS=tests/support/consume_plane/fixtures/velocity_burst.jsonl
DEMO_CONTRACTS=tests/support/consume_plane/fixtures/velocity_burst.contracts.jsonl
EVENTS=${1:-$DEMO_EVENTS}
CONTRACTS=${2:-}
if [[ -z "$CONTRACTS" && "$EVENTS" == "$DEMO_EVENTS" ]]; then
    CONTRACTS=$DEMO_CONTRACTS
elif [[ -z "$CONTRACTS" && -f "${EVENTS%.jsonl}.contracts.jsonl" ]]; then
    CONTRACTS="${EVENTS%.jsonl}.contracts.jsonl"   # convention: <name>.jsonl + <name>.contracts.jsonl
fi
CONFIG=${CONFIG:-consume_plane.yaml}
LEDGER=${LEDGER:-:memory:}

for f in "$EVENTS" "$CONFIG" ${CONTRACTS:+"$CONTRACTS"}; do
    [[ -f "$f" ]] || { echo "error: file not found: $f" >&2; exit 1; }
done

# The JSONL sink appends; clear the bundled demos' previous findings so the output reflects this run only.
if [[ "$EVENTS" == tests/support/consume_plane/fixtures/* ]]; then
    rm -f runs/run_demo/findings.jsonl
fi

uv sync --quiet

args=(--config "$CONFIG" --replay "$EVENTS" --ledger "$LEDGER" --log-level "${LOG_LEVEL:-INFO}")
[[ -n "$CONTRACTS" ]] && args+=(--contracts "$CONTRACTS")

echo "== consume plane: replaying $EVENTS"
status=0
uv run python -m consume_plane "${args[@]}" || status=$?

echo
echo "== findings written by the JSONL sink"
shopt -s nullglob
files=(runs/*/findings.jsonl)
if (( ${#files[@]} == 0 )); then
    echo "(none)"
else
    for f in "${files[@]}"; do
        echo "-- $f"
        uv run python -c '
import json, sys
fmt = "  [{severity:>8}] {rule_id:<24} {plugin}@{plugin_version}  session={session_id} trigger={trigger_event_id}  {summary}"
for line in open(sys.argv[1]):
    print(fmt.format(**json.loads(line)))
' "$f"
    done
fi
exit "$status"
