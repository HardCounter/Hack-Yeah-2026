#!/usr/bin/env bash
# Automated check that real OpenCode loads the adapter and reaches the Python receiver.
# Starts a receiver on a free port, starts OpenCode (private server, pseudo-terminal) in a throwaway
# project for a few seconds, and passes if the adapter's startup handshake arrives.
# No prompt is sent, so no model provider is contacted by this check.
#
# Usage: scripts/check_opencode_pipeline.sh     (WAIT=25 seconds max)
set -euo pipefail

cd "$(dirname "$0")/.."
REPO=$(pwd)
unset VIRTUAL_ENV
WAIT=${WAIT:-25}

for cmd in uv opencode script; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "error: $cmd is required" >&2; exit 1; }
done

WORK=$(mktemp -d)
cleanup() {
    [[ -n "${OC_PID:-}" ]] && kill "$OC_PID" 2>/dev/null || true
    [[ -n "${RX_PID:-}" ]] && kill "$RX_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

PORT=$(uv run --quiet python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
export INTERCEPT_TOKEN=$(uv run --quiet python -c 'import secrets; print(secrets.token_hex(24))')
mkdir -p "$WORK/project"
cat > "$WORK/project/opencode.json" <<EOF
{ "\$schema": "https://opencode.ai/config.json",
  "plugins": [{ "package": "$REPO/adapters/opencode",
                "options": { "endpoint": "http://127.0.0.1:$PORT", "prompts": "observe" } }] }
EOF

echo "== starting receiver on 127.0.0.1:$PORT"
uv run python -m intercept.receiver --port "$PORT" --no-color > "$WORK/receiver.log" 2>&1 &
RX_PID=$!
for _ in $(seq 1 50); do (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break; sleep 0.1; done

echo "== starting OpenCode (private server, no prompt) in a throwaway project"
( cd "$WORK/project" &&
  script -qfc "opencode --standalone --print-logs --log-level info 2>'$WORK/opencode.log'" /dev/null \
      </dev/null >/dev/null 2>&1 ) &
OC_PID=$!

result=""
for _ in $(seq 1 $((WAIT * 4))); do
    # match the handshake event lines, not the receiver's startup banner
    if grep -q "ADAPTER CONNECTED hardcounter.intercept" "$WORK/receiver.log"; then result=pass; break; fi
    if grep -q "ADAPTER FAILED TO START status=" "$WORK/receiver.log"; then result=fail; break; fi
    sleep 0.25
done

echo
grep -A1 -E "ADAPTER (CONNECTED hardcounter|FAILED TO START status=)" "$WORK/receiver.log" || true
grep -E 'loading plugin|hardcounter.intercept|Plugin failed' "$WORK/opencode.log" 2>/dev/null | cut -c1-300 || true
echo
case "$result" in
    pass) echo "PASS: OpenCode loaded the adapter and it reached the Python receiver."; exit 0 ;;
    fail) echo "FAIL: the adapter loaded but its setup failed (reason above)."; exit 1 ;;
    *)    echo "FAIL: no handshake within ${WAIT}s. OpenCode log tail:"; tail -20 "$WORK/opencode.log" 2>/dev/null | cut -c1-300; exit 1 ;;
esac
