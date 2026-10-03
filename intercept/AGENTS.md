# Interception implementation boundaries

- Use Python for all implementation, services, auditor plugins, configuration loading,
  persistence, supervision, verification, demos, and tests outside the OpenCode adapter.
- JavaScript/TypeScript is permitted only inside `adapters/opencode/`, where OpenCode's
  plugin runtime requires it. Prefer the minimal JavaScript shim; keep policy authority
  and business logic in Python. Do not introduce JavaScript/TypeScript elsewhere.
- Use Python asyncio and uv, following `docs/stack.md`.
- Keep configured hard checks before semantic work, and revalidate transformed payloads.
- Do not treat observed tool results or HTTP success as independently verified outcomes.
