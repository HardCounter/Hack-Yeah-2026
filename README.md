# AI Control Layer

A policy-driven runtime control and verification layer for autonomous AI agents.

The system is intended to sit between agents and LLMs, MCP servers, APIs, tools, and other agents. It combines deterministic interaction enforcement, task-level trajectory supervision, and independent verification of the resulting external state under a shared Task Contract and centralized policy.

```text
Is the action authorized?       -> deterministic policy
Does it serve the task?         -> trajectory supervision
Did the correct result exist?  -> independent outcome verification
```

### Current Implementation State
- **Implemented so far:** The synthetic banking dataset generator (`data/generate.py`, `data/rules.py`, `data/report.py`), the KYC outcome verifier (`data/postconditions.py`), and the 16 agent tools (`sim/tools/`, of which 6 are **bait tools: fakes** that only exist so the control layer has something to intercept/block).
- **Architecture & Runtime Scope:** The MVP scope is **KYC only**; AML is deferred. The runtime gateway proxy, live semantic supervisor, and judge dashboard are currently specifications in `docs/` and have not been implemented yet. Planned bait tools are simulations writing to `audit_actions` only, not real email, network, code-execution, or deletion integrations.

## Documentation Index

- [Project direction](docs/project-direction.md)
- [Required runtime architecture contract](docs/architecture-contract.md)
- [Architecture review findings and remaining gates](docs/architecture-review.md)
- [OpenCode plugin adapter plan](docs/intercept/opencode-adapter-plan.md)
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

## Python Development & Testing with uv

We use **uv** for environment management, dependency locking, and Python execution.
Install uv separately, then from the repository root:

```sh
uv sync --locked
uv run --locked python --version
uv lock --check
```

`pyproject.toml` allows Python >=3.11; `.python-version` pins local development to 3.12.
Dev dependencies include `pytest`. Runtime framework choices for the gateway remain open.

### Running the Suite

```sh
# Build the synthetic dataset (deterministic, seed 2026)
uv run --locked python data/generate.py

# Render the dataset explorer report
uv run --locked python data/report.py

# Run the test suite
uv run --locked pytest
```

CI (`.github/workflows/tests.yml`) runs on every push across Linux and Windows. `sim/tools/test_ollama.py` tests tool-calling with a local model (`ollama pull llama3.2`) and automatically skips itself when Ollama is unavailable.

Add a test with every change: a `test_*` function next to the code it checks (`data/`, `sim/`, `tests/`).
