#!/usr/bin/env bash
# Offline synthetic gateway runs + real read-only REST API; no model/provider required.
set -euo pipefail
cd "$(dirname "$0")/.."
unset VIRTUAL_ENV
command -v uv >/dev/null 2>&1 || { echo "error: uv is not installed" >&2; exit 1; }
exec uv run --locked python -m persistence.http_api.demo "$@"
