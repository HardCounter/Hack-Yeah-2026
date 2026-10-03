# AI Control Layer

A policy-driven runtime control and verification layer for autonomous AI agents.

The system is intended to sit between agents and LLMs, MCP servers, APIs, tools, and other agents. It combines deterministic interaction enforcement, task-level trajectory supervision, and independent verification of the resulting external state under a shared Task Contract and centralized policy.

```text
Is the action authorized?       -> deterministic policy
Does it serve the task?         -> trajectory supervision
Did the correct result exist?  -> independent outcome verification
```

This repository currently contains competition and architecture documents, implemented
stdlib-only synthetic banking data scripts (`data/generate.py`, `data/rules.py`,
`data/report.py`), Python/uv project metadata, and OpenCode development/review configuration.
Runtime gateway, policy engine, semantic supervisor, outcome verifier, dashboard and
executable product tests are **not present yet**. The MVP scope is **KYC only**; AML is deferred.
Planned bait tools are fakes, not real email, network, code-execution or deletion integrations.

- [Project direction](docs/project-direction.md)
- [Required runtime architecture contract](docs/architecture-contract.md)
- [Architecture review findings and remaining gates](docs/architecture-review.md)
- [Monitored banking use cases](docs/use-cases.md)
- [Mock banking dataset specification](docs/mock-data-spec.md)
- [System architecture & execution lifecycle](docs/system-architecture.md)
- [Control Gateway & Interception layer specification](docs/application-documentation.md)
- [Judge dashboard UI design](docs/dashboard-ui.md)
- [Technical challenge and criteria](GoldmanSachsCriteria.md)
- [Competition rules](GoldmanSachsRules.md)
- [OpenCode agents, NVIDIA setup, and review commands](.opencode/README.md)
- [Shared engineering instructions](AGENTS.md)

The Rules and Criteria disagree on self-testing and scalability scoring weights. Preserve both sources and confirm the applicable weights with the organizer.

## Python development with uv

We use **uv** for environment management, dependency locking and Python execution.
Install uv separately, then from the repository root:

```sh
uv sync --locked
uv run --locked python --version
uv lock --check
```

`pyproject.toml` allows Python >=3.11; `.python-version` pins local development to 3.12.
Commit `uv.lock` when adding real dependencies; the current project has no third-party
dependencies. Runtime framework choices remain open. The existing synthetic scripts run with:

```sh
uv run --locked python data/generate.py
uv run --locked python data/report.py
```

Generation overwrites the ignored `data/bank.db`, `data/ground_truth.json` and document
fixtures; reporting writes `data/report.html`. Use synthetic data only. These commands
are dataset utilities, **not** control-layer tests or proof of external business outcomes.
There is no judge-runnable product suite yet.
