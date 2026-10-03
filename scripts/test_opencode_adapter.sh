#!/usr/bin/env bash
# Run the OpenCode adapter tests. The forwarding test prints every JSON request the adapter
# sends to the control service (prompts, LLM requests, tool admissions and outcomes).
#
# Usage: scripts/test_opencode_adapter.sh          # forwarding test with request log + adapter unit tests
#        scripts/test_opencode_adapter.sh --quiet  # same, without the request log
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v node >/dev/null 2>&1; then
    echo "error: node is not installed (Node.js 22+ required)" >&2
    exit 1
fi
major=$(node -p 'process.versions.node.split(".")[0]')
if (( major < 22 )); then
    echo "error: Node.js 22+ required, found $(node --version)" >&2
    exit 1
fi

if [[ "${1:-}" == "--quiet" ]]; then
    export FORWARD_QUIET=1
fi

# vm.SourceTextModule loads the plugin with a stub for @opencode/plugin, so no npm install is needed.
NODE_OPTS=(--experimental-vm-modules --no-warnings --test --test-reporter=spec)

echo "== OpenCode adapter: forwarding test (requests sent to the control service)"
node "${NODE_OPTS[@]}" adapters/opencode/forward.test.mjs

echo
echo "== OpenCode adapter: unit tests"
node "${NODE_OPTS[@]}" adapters/opencode/test.mjs
