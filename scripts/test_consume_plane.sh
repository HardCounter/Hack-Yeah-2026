#!/usr/bin/env bash
# Run the consume plane (Layer 3) test suite.
# Usage: scripts/test_consume_plane.sh [extra pytest args]   e.g. -k feedback, -x, -v
set -euo pipefail

cd "$(dirname "$0")/.."
unset VIRTUAL_ENV  # use the project's .venv managed by uv

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is not installed (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

uv sync --quiet
exec uv run pytest tests/support/consume_plane -q "$@"
