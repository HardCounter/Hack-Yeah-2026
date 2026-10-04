#!/usr/bin/env bash
# Run the read-only REST API for dashboards on its own (docs/rest.md). It is currently a stub that
# serves example data, so dashboard work can start without a live pipeline.
# scripts/run_live_pipeline.sh already starts the API next to the gateway; use this script only
# without a pipeline.
#
# Usage: scripts/run_rest_api.sh [--port 8790] [--cors-origin http://localhost:5173] [--evidence-dir DIR]
#   No authentication: it binds 127.0.0.1 unless --host and --allow-remote are given.
#   Interactive docs: http://127.0.0.1:8790/api/v1/docs
set -euo pipefail

cd "$(dirname "$0")/.."
unset VIRTUAL_ENV
command -v uv >/dev/null 2>&1 || { echo "error: uv is not installed" >&2; exit 1; }
exec uv run --locked python -m persistence.http_api "$@"
