#!/usr/bin/env bash
# Terminal 2: start OpenCode wired to the service started in terminal 1, either
# scripts/run_live_pipeline.sh (governed pipeline) or scripts/run_intercept_receiver.sh (observe-only).
#
# Usage: scripts/run_opencode_intercepted.sh [--workspace DIR] [extra opencode flags, e.g. --print-logs]
#
# Why this script: the adapter runs inside OpenCode's *server* process and needs INTERCEPT_TOKEN there.
# --standalone starts a private server that inherits this environment; the shared background service
# would not have the token, and the plugin would fail to start.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=var/intercept.env

[[ -f "$ENV_FILE" ]] || { echo "error: $ENV_FILE not found; start scripts/run_live_pipeline.sh or scripts/run_intercept_receiver.sh first" >&2; exit 1; }
command -v opencode >/dev/null 2>&1 || { echo "error: opencode is not on PATH" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

# Keep OpenCode's run-specific config in the temporary demo project, independently of the selected
# workspace. This prevents a workspace's opencode.json from replacing the interception config.
WORKSPACE=${INTERCEPT_DEMO_DIR}
OPENCODE_ARGS=()
while (($#)); do
    case "$1" in
        --workspace)
            [[ $# -ge 2 && -n "$2" ]] || { echo "error: --workspace requires a directory" >&2; exit 2; }
            WORKSPACE=$2
            shift 2
            ;;
        --workspace=*)
            WORKSPACE=${1#--workspace=}
            [[ -n "$WORKSPACE" ]] || { echo "error: --workspace requires a directory" >&2; exit 2; }
            shift
            ;;
        *)
            OPENCODE_ARGS+=("$1")
            shift
            ;;
    esac
done

[[ -d "$WORKSPACE" ]] || { echo "error: workspace directory does not exist: $WORKSPACE" >&2; exit 2; }
WORKSPACE=$(cd -- "$WORKSPACE" && pwd -P)

if ! (exec 3<>"/dev/tcp/127.0.0.1/$INTERCEPT_PORT") 2>/dev/null; then
    echo "error: no receiver on 127.0.0.1:$INTERCEPT_PORT; start scripts/run_intercept_receiver.sh first" >&2
    exit 1
fi
grep -q '"package": ".*adapters/opencode"' "$INTERCEPT_DEMO_DIR/opencode.json" 2>/dev/null ||
    { echo "error: $INTERCEPT_DEMO_DIR/opencode.json does not load the adapter" >&2; exit 1; }

echo "Starting OpenCode in $WORKSPACE (private server, adapter -> 127.0.0.1:$INTERCEPT_PORT)"
cd "$WORKSPACE"
# The admin token is present only for the live governed pipeline (used by /intercept-run to bind the session).
INTERCEPT_TOKEN=$INTERCEPT_TOKEN \
INTERCEPT_ADMIN_TOKEN=${INTERCEPT_ADMIN_TOKEN:-} \
OPENCODE_CONFIG="$INTERCEPT_DEMO_DIR/opencode.json" \
OPENCODE_CONFIG_DIR="$INTERCEPT_DEMO_DIR/.opencode" \
OPENCODE_DISABLE_PROJECT_CONFIG=1 \
exec opencode --standalone "${OPENCODE_ARGS[@]}"
