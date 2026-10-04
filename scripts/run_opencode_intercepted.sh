#!/usr/bin/env bash
# Terminal 2: start OpenCode in the current directory, wired to the service started in terminal 1,
# either scripts/run_live_pipeline.sh (governed pipeline) or scripts/run_intercept_receiver.sh (observe-only).
#
# Usage: scripts/run_opencode_intercepted.sh [--workspace DIR] [extra opencode flags, e.g. --print-logs]
# Usage: scripts/run_opencode_intercepted.sh [extra opencode flags, e.g. --print-logs]
#        OPEN_IN_PROJECT=1 scripts/run_opencode_intercepted.sh   # open in the prepared project dir instead
#
# How it attaches without changing directory:
#   OPENCODE_CONFIG       the prepared opencode.json (loads the adapter plugin with its options)
#   OPENCODE_CONFIG_DIR   only for the governed pipeline: a temporary copy of your global config dir
#                         (entries symlinked) plus the governed `onboarding-agent` definition, which
#                         OpenCode reads only from an agents/ folder; removed when OpenCode exits.
#   --standalone          private server that inherits INTERCEPT_TOKEN; the shared background service
#                         would not have it and the plugin would fail to start.
# Instructions files (AGENTS.md) of the directory you open in are added to the agent's context.
set -euo pipefail

START_DIR=$PWD
cd "$(dirname "$0")/.."
ENV_FILE=$PWD/var/intercept.env

[[ -f "$ENV_FILE" ]] || { echo "error: $ENV_FILE not found; start scripts/run_live_pipeline.sh or scripts/run_intercept_receiver.sh first" >&2; exit 1; }
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

# Checked after the arguments, so a usage error is reported even on a machine without OpenCode.
command -v opencode >/dev/null 2>&1 || { echo "error: opencode is not on PATH" >&2; exit 1; }

if ! (exec 3<>"/dev/tcp/127.0.0.1/$INTERCEPT_PORT") 2>/dev/null; then
    echo "error: nothing listens on 127.0.0.1:$INTERCEPT_PORT; start terminal 1 first" >&2
    exit 1
fi
CONFIG_FILE=$INTERCEPT_DEMO_DIR/opencode.json
grep -q '"package": ".*adapters/opencode"' "$CONFIG_FILE" 2>/dev/null ||
    { echo "error: $CONFIG_FILE does not load the adapter" >&2; exit 1; }

echo "Starting OpenCode in $WORKSPACE (private server, adapter -> 127.0.0.1:$INTERCEPT_PORT)"
cd "$WORKSPACE"
# The admin token is present only for the live governed pipeline (used by /intercept-run to bind the session).
INTERCEPT_TOKEN=$INTERCEPT_TOKEN \
INTERCEPT_ADMIN_TOKEN=${INTERCEPT_ADMIN_TOKEN:-} \
OPENCODE_CONFIG="$INTERCEPT_DEMO_DIR/opencode.json" \
OPENCODE_CONFIG_DIR="$INTERCEPT_DEMO_DIR/.opencode" \
OPENCODE_DISABLE_PROJECT_CONFIG=1 \
exec opencode --standalone "${OPENCODE_ARGS[@]}"
export INTERCEPT_TOKEN
export INTERCEPT_ADMIN_TOKEN=${INTERCEPT_ADMIN_TOKEN:-}  # governed pipeline only: /intercept-run binds the session

if [[ "${OPEN_IN_PROJECT:-0}" == "1" ]]; then
    echo "Starting OpenCode in $INTERCEPT_DEMO_DIR (adapter -> 127.0.0.1:$INTERCEPT_PORT)"
    cd "$INTERCEPT_DEMO_DIR"
    exec opencode --standalone "$@"
fi

export OPENCODE_CONFIG=$CONFIG_FILE
CONFIG_DIR=""
AGENTS_SRC=$INTERCEPT_DEMO_DIR/.opencode/agents
if [[ -d "$AGENTS_SRC" ]]; then
    GLOBAL_DIR=${OPENCODE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}
    CONFIG_DIR=$(mktemp -d "${TMPDIR:-/tmp}/opencode-config-XXXXXX")
    trap 'rm -rf "$CONFIG_DIR"' EXIT
    shopt -s nullglob dotglob
    for entry in "$GLOBAL_DIR"/*; do
        [[ "$(basename "$entry")" == agents ]] || ln -s "$entry" "$CONFIG_DIR/"
    done
    mkdir "$CONFIG_DIR/agents"
    for agent in "$GLOBAL_DIR"/agents/* "$AGENTS_SRC"/*; do ln -sf "$agent" "$CONFIG_DIR/agents/"; done
    shopt -u nullglob dotglob
    export OPENCODE_CONFIG_DIR=$CONFIG_DIR
fi

echo "Starting OpenCode in $START_DIR (adapter -> 127.0.0.1:$INTERCEPT_PORT)"
cd "$START_DIR"
opencode --standalone "$@"
