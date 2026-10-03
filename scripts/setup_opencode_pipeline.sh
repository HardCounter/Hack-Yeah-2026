#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf '%s\n' \
        'Install the locked Python environment and a project-local OpenCode CLI.' \
        '' \
        'Usage: scripts/setup_opencode_pipeline.sh' \
        '' \
        'Requires uv, npm, and network access to their public package registries. The' \
        'OpenCode package is installed under ignored var/opencode-cli; no global install' \
        'or credential files are changed.'
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi
if [[ $# -ne 0 ]]; then
    usage >&2
    exit 2
fi

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL_DIR="$ROOT/var/opencode-cli"
PINNED_VERSION=2.0.22

cd "$ROOT"
command -v uv >/dev/null 2>&1 || { echo "error: uv is required" >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "error: npm is required to install the pinned OpenCode CLI" >&2; exit 1; }

unset VIRTUAL_ENV
uv sync --locked

LOCAL_BIN="$INSTALL_DIR/node_modules/.bin/opencode"
if [[ -x "$LOCAL_BIN" ]]; then
    current_version=$("$LOCAL_BIN" --version 2>/dev/null || true)
    current_help=$("$LOCAL_BIN" run --help 2>/dev/null || true)
    if [[ "$current_version" =~ (^|[[:space:]v])${PINNED_VERSION//./\\.}([[:space:]]|$) &&
          "$current_help" == *"--standalone"* && "$current_help" == *"--session"* ]]; then
        echo "OpenCode CLI $PINNED_VERSION is already installed in var/opencode-cli."
        exit 0
    fi
fi

mkdir -p "$INSTALL_DIR"
npm install --prefix "$INSTALL_DIR" --no-save --no-package-lock --no-audit --no-fund \
    "@opencode/cli@$PINNED_VERSION"

[[ -x "$LOCAL_BIN" ]] || { echo "error: pinned OpenCode package did not install its CLI" >&2; exit 1; }
installed_version=$("$LOCAL_BIN" --version)
installed_help=$("$LOCAL_BIN" run --help)
[[ "$installed_version" =~ (^|[[:space:]v])${PINNED_VERSION//./\\.}([[:space:]]|$) &&
   "$installed_help" == *"--standalone"* && "$installed_help" == *"--session"* ]] || {
    echo "error: installed OpenCode CLI is not the pinned standalone-capable $PINNED_VERSION release" >&2
    exit 1
}
echo "Installed OpenCode CLI $PINNED_VERSION in ignored var/opencode-cli."
