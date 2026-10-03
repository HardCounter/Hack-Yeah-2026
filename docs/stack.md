# Implementation stack

- **Python 3.11+ and asyncio** for the interception service, centralized policy,
  Task Contracts, resource accounting, asynchronous persistence, and verification.
- **uv** for Python environment/dependency management and execution; use the
  repository's `pyproject.toml` and `uv.lock`. No alternate Python package manager.
- **OpenCode v2.0.22** is the first and only agent-runtime adapter target.
  Its plugin API requires JavaScript/TypeScript; a minimal **JavaScript** shim
  registers tool hooks and calls the Python service. No TypeScript is needed.
  No policy authority or business-verification logic belongs in that shim.

The initial control service uses the Python standard library and a bounded asyncio
ingestion queue. Disk append runs via `asyncio.to_thread` so it does not block the
event loop. This is not a durable queue or a production HTTP server. See
[initial slice status](intercept/implementation-status.md) for actual coverage.
