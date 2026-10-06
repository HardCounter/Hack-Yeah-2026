# Contributing

Use Python 3.12, uv, and Node.js 22+ from the repository root. Python 3.11+ is supported;
`.python-version` selects the development interpreter. Python dependencies are locked in `uv.lock`.

```sh
uv sync --locked
uv lock --check
uv run --locked ruff check .
uv run --locked pytest -q
node --experimental-vm-modules --test tests/frontend/dashboard.test.mjs adapters/opencode/test.mjs adapters/opencode/forward.test.mjs
```

`scripts/test.sh all` combines these checks on POSIX systems. Use `scripts/test.sh --help` for
layer-specific targets; `scripts/test.sh lint` runs the focused Python correctness checks.
Ruff checks syntax, undefined names, invalid comparisons and accidental redefinitions. The selected
rules intentionally do not prescribe a repository-wide formatting style.

Start with the [offline quickstart](README.md#try-it-without-a-model). Tests generate isolated synthetic
state in temporary directories. Do not use real banking/customer data, credentials, or private keys
as fixtures. Keep `.env`, execution artifacts, Terraform state and authentication storage out of Git.

The normal suite does not need a hosted model. Tests that require a real OpenCode executable skip
when it is absent. To enable them, install it with `scripts/setup_opencode_pipeline.sh`; its loopback
fixture model does not contact a provider. Live-provider checks are separate and require explicit
`RUN_LLM_TESTS=1` plus your own provider configuration.

Preserve the three boundaries: deterministic admission before dispatch, contextual supervision of
observable events, and independent verification of persisted state. Keep policy authority in Python;
OpenCode-specific runtime glue belongs in `adapters/opencode/`. Read directory `AGENTS.md` files
before editing their scope. Proposed semantic controls must never weaken hard denials or budgets.

For a behavior fix, provide a regression that demonstrates the failure and the resulting behavior.
Run affected checks, then the full suite when a shared contract or persistence/runtime path changes.
Keep pull requests focused, explain the concrete trigger and effect, and state what was verified.
Distinguish proposed features, simulated evidence and live integration results in documentation.

CI runs Python and Node checks on three operating systems and builds the deployment image on Linux.
Terraform applies and the `deploy` branch workflow operate external infrastructure and are separate
from local development. Moving the `deploy` branch starts a release.

The team has not selected a project license. Do not introduce a license or claim individual ownership
of the team's work without the maintainers' agreement; dependency metadata is listed in [THIRD_PARTY.md](THIRD_PARTY.md).
