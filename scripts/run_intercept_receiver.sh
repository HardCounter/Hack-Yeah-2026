#!/usr/bin/env bash
# Terminal 1: start the observe-only Python receiver that logs every OpenCode adapter request.
# Terminal 2: scripts/run_opencode_intercepted.sh   (starts OpenCode wired to this receiver)
#
# Environment:
#   PORT=8080            receiver port (loopback only)
#   PROMPTS=observe      adapter prompt forwarding: observe | enforce | off (tool calls are always forwarded)
#   DEMO_DIR=var/opencode-demo   OpenCode project whose opencode.json loads the adapter (rewritten here)
#   COMPACT=1            one JSON line per request
#
# The token and port are written to var/intercept.env (mode 600) so the OpenCode launcher uses
# exactly the same values. OBSERVE-ONLY: the receiver logs and ALLOWS everything.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO=$(pwd)
unset VIRTUAL_ENV  # use the project's .venv managed by uv

PORT=${PORT:-8080}
PROMPTS=${PROMPTS:-observe}
DEMO_DIR=${DEMO_DIR:-var/opencode-demo}
ENV_FILE=var/intercept.env

command -v uv >/dev/null 2>&1 || { echo "error: uv is not installed" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "error: PORT must be a number" >&2; exit 1; }
[[ "$PROMPTS" =~ ^(observe|enforce|off)$ ]] || { echo "error: PROMPTS must be observe, enforce or off" >&2; exit 1; }
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    echo "error: port $PORT is already in use; stop that process or set PORT=..." >&2
    exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
    owner=$(sed -n 's/^INTERCEPT_PORT=//p' "$ENV_FILE")
    if [[ -n "$owner" ]] && (exec 3<>"/dev/tcp/127.0.0.1/$owner") 2>/dev/null; then
        echo "error: another pipeline/receiver is running on port $owner ($ENV_FILE); stop it first" >&2
        exit 1
    fi
fi

# Fresh token per receiver run, shared with the OpenCode launcher through a private file.
mkdir -p var "$DEMO_DIR"
TOKEN=$(uv run --quiet python -c 'import secrets; print(secrets.token_hex(24))')
DEMO_ABS=$(cd "$DEMO_DIR" && pwd)
( umask 077; printf 'INTERCEPT_TOKEN=%s\nINTERCEPT_PORT=%s\nINTERCEPT_DEMO_DIR=%s\n' "$TOKEN" "$PORT" "$DEMO_ABS" > "$ENV_FILE" )
export INTERCEPT_TOKEN=$TOKEN

# OpenCode project for the manual run: its config loads the adapter straight from this repository.
cat > "$DEMO_ABS/opencode.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "plugins": [
    {
      "package": "$REPO/adapters/opencode",
      "options": { "endpoint": "http://127.0.0.1:$PORT", "prompts": "$PROMPTS" }
    }
  ]
}
EOF
if [[ ! -f "$DEMO_ABS/notes.md" ]]; then
    printf '# Synthetic demo file\n\nApplication APP-0001 is waiting for review. Use synthetic data only.\n' > "$DEMO_ABS/notes.md"
fi

cat <<EOF
================================================================================
 OpenCode -> Python interception check (observe-only)
================================================================================
 In a SECOND terminal run:

   scripts/run_opencode_intercepted.sh

 1. "ADAPTER CONNECTED" appears here as soon as OpenCode loads the plugin
    (before any prompt). "ADAPTER FAILED TO START" shows why it did not.
 2. Send e.g. "Read notes.md and summarise it": expect a user prompt,
    llm_request(s), then evaluate/outcome per tool call.
================================================================================

EOF

args=(--port "$PORT")
[[ "${COMPACT:-0}" == "1" ]] && args+=(--compact)
exec uv run python -m intercept.service.receiver "${args[@]}"
