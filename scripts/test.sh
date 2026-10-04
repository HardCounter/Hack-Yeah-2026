#!/usr/bin/env bash
# One entry point for the control-layer tests.
#
# Usage: scripts/test.sh [target] [extra pytest args]
#   all          (default) every Python test + OpenCode adapter Node tests
#   python       every Python test (uv run pytest)
#   controls     tests/control_layer: the guardrail suite (tool calls and prompts through the gateway)
#   support      tests/support: everything else (plumbing, audit store, config API, consume plane, web app, mock bank)
#   intercept    Layer 1: policy, auditors, HTTP service, governed gateway and prompts
#   persistence  Layer 2: store, outbox, writer/reader, v2.1 encoder
#   consume      Layer 3: consume plane runtime and plugins
#   e2e          cross-layer and real-OpenCode pipeline tests (+ live adapter handshake check)
#   adapter      OpenCode adapter Node tests (no npm install needed)
#   data         dataset generator, KYC tools and outcome postconditions
#   decisions    control-plane decision trace: model, manager, store, REST and running scenarios
# Examples: scripts/test.sh persistence -x      scripts/test.sh intercept -k prompt
set -euo pipefail

cd "$(dirname "$0")/.."
unset VIRTUAL_ENV  # use the project's .venv managed by uv
command -v uv >/dev/null 2>&1 || { echo "error: uv is not installed" >&2; exit 1; }

target=${1:-all}
[[ $# -gt 0 ]] && shift
pytest() { echo "== pytest $*"; uv run --locked pytest -q "$@"; }

case "$target" in
    all)         pytest "$@"; echo; scripts/test_opencode_adapter.sh --quiet ;;
    python)      pytest "$@" ;;
    intercept)   pytest intercept tests/test_governed_gateway.py tests/test_governed_prompts.py \
                        tests/test_integrated_http.py tests/test_integrated_llm.py "$@" ;;
    persistence) pytest tests/test_persistence*.py tests/test_pipeline_support.py "$@" ;;
    consume)     pytest tests/consume_plane "$@" ;;
    decisions)   pytest tests/consume_plane/test_decision_model.py tests/consume_plane/test_decisions.py \
                        tests/test_decision_trace_api.py tests/test_decisions_demo.py "$@" ;;
    e2e)         pytest tests/test_three_layer_e2e.py tests/test_integrated_regressions.py \
                        tests/test_opencode_pipeline.py tests/test_pipeline_scripts.py "$@"
                 if command -v opencode >/dev/null 2>&1 && command -v script >/dev/null 2>&1; then
                     echo; scripts/check_opencode_pipeline.sh
                 else
                     echo "skipped: live adapter handshake check (needs opencode and script on PATH)"
                 fi ;;
    adapter)     scripts/test_opencode_adapter.sh "$@" ;;
    data)        pytest tests/support/test_postconditions.py tests/support/test_agent.py tests/support/test_tools.py "$@" ;;
    -h|--help)   sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)           echo "error: unknown target '$target' (try --help)" >&2; exit 2 ;;
esac
