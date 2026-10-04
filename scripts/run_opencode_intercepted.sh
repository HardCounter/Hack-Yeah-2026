#!/usr/bin/env bash
# Terminal 2: start OpenCode wired to the receiver started by scripts/run_intercept_receiver.sh.
#
# Usage: scripts/run_opencode_intercepted.sh [extra opencode flags, e.g. --print-logs]
#
# Why this script: the adapter runs inside OpenCode's *server* process and needs INTERCEPT_TOKEN there.
# --standalone starts a private server that inherits this environment; the shared background service
# would not have the token, and the plugin would fail to start.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=var/intercept.env

REPO=$(pwd)
if [[ -x "$REPO/var/opencode-cli/node_modules/.bin/opencode" ]]; then
    export PATH="$REPO/var/opencode-cli/node_modules/.bin:$PATH"
fi

[[ -f "$ENV_FILE" ]] || { echo "error: $ENV_FILE not found; start scripts/run_intercept_receiver.sh first" >&2; exit 1; }
command -v opencode >/dev/null 2>&1 || { echo "error: opencode is not on PATH" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

if ! (exec 3<>"/dev/tcp/127.0.0.1/$INTERCEPT_PORT") 2>/dev/null; then
    echo "error: no receiver on 127.0.0.1:$INTERCEPT_PORT; start scripts/run_intercept_receiver.sh first" >&2
    exit 1
fi
grep -q '"package": ".*adapters/opencode"' "$INTERCEPT_DEMO_DIR/opencode.json" 2>/dev/null ||
    { echo "error: $INTERCEPT_DEMO_DIR/opencode.json does not load the adapter" >&2; exit 1; }

echo "Starting OpenCode in $INTERCEPT_DEMO_DIR (private server, adapter -> 127.0.0.1:$INTERCEPT_PORT)"
cd "$INTERCEPT_DEMO_DIR"
INTERCEPT_TOKEN=$INTERCEPT_TOKEN exec opencode --standalone "$@"
