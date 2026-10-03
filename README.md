# AI Control Layer

A policy-driven runtime control and verification layer for autonomous AI agents.

The system is intended to sit between agents and LLMs, MCP servers, APIs, tools, and other agents. It combines deterministic interaction enforcement, task-level trajectory supervision, and independent verification of the resulting external state under a shared Task Contract and centralized policy.

```text
Is the action authorized?       -> deterministic policy
Does it serve the task?         -> trajectory supervision
Did the correct result exist?  -> independent outcome verification
```

### Current Implementation State
- **Implemented so far:** The synthetic banking dataset generator (`data/generate.py`, `data/rules.py`, `data/report.py`), the KYC outcome verifier (`data/postconditions.py`), and the 16 agent tools (`sim/tools/` and `simulation/tools/`, of which 6 are **bait tools: fakes** that only exist so the control layer has something to intercept/block).
- **Interception Layer:** Initial Python asyncio interception service with configurable allowlist, signature-scanner, and webhook auditors (`intercept/`).
- **OpenCode Adapter:** JavaScript plugin for OpenCode v2.0.22 (`adapters/opencode/`) that forwards tool calls (always) and prompts/model requests (opt-in) to the Python service. Real OpenCode loads it, which the startup handshake verifies. `intercept/receiver.py` is an observe-only Python server for manual checks: it logs and allows everything. See [OpenCode forwarding](docs/intercept/opencode-forwarding.md).
- **Integrated KYC runtime:** simulation tool proposals now pass through Layer 1, durable Layer 2 evidence/outbox and the existing ConsumerManager with trajectory-risk feedback and lifecycle outcome verification. Both drivers share the governed path. See [local execution and limits](docs/integrated-runtime.md).
- **Durable Persistence:** `persistence/` supplies sanitized immutable SQLite evidence, atomic outbox commits, bounded consumer delivery with DLQ, run binding and contiguous action indexing, atomic banking receipts and replication, scoped readers with keyset pagination, and maintenance/backup facilities. See [integration and limits](docs/persistence.md).
- **Architecture & Runtime Scope:** The MVP scope is **KYC only**; AML is deferred.

## Documentation Index

- [Integrated local KYC execution](docs/integrated-runtime.md)
- [Project direction](docs/project-direction.md)
- [Required runtime architecture contract](docs/architecture-contract.md)
- [Architecture review findings and remaining gates](docs/architecture-review.md)
- [Persistence integration and guarantees](docs/persistence.md)
- [Local changes review and requirement gaps](docs/local-changes-review.md)
- [OpenCode plugin adapter plan](docs/intercept/opencode-adapter-plan.md)
- [OpenCode adapter: forwarded requests, receiver, manual validation](docs/intercept/opencode-forwarding.md)
- [Interception slice status](docs/intercept/implementation-status.md)
- [Consume plane design](docs/consumer-plane.md), [implementation notes](docs/consumer-plane-implementation-notes.md), [event envelope v2.1](docs/consumer-plane-event-envelope.md)
- [Trajectory risk model](docs/trajectory-risk-model.md)
- [Implementation stack: Python asyncio and uv](docs/stack.md)
- [Monitored banking use cases](docs/use-cases.md)
- [Mock banking dataset specification](docs/mock-data-spec.md)
- [System architecture & execution lifecycle](docs/system-architecture.md)
- [Control Gateway & Interception layer specification](docs/application-documentation.md)
- [Judge dashboard UI design](docs/dashboard/dashboard-ui.md)
- [Technical challenge and criteria](docs/goldman/GoldmanSachsCriteria.md)
- [Competition rules](docs/goldman/GoldmanSachsRules.md)
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
Dev dependencies include `pytest`. The integrated local gateway uses Python asyncio and SQLite.

### Running the Suite

```sh
# Build the synthetic dataset (deterministic, seed 2026)
uv run --locked python data/generate.py

# Render the dataset explorer report
uv run --locked python data/report.py

# Run the governed end-to-end demo
uv run python -m simulation.agent APP-0001 --driver scripted

# Run the complete test suite (offline and locked)
uv run --locked --offline pytest

# Run the persistence demo CLI (all failure and recovery scenarios)
uv run python -m persistence.demo --scenario clean
uv run python -m persistence.demo --scenario audit-unavailable
uv run python -m persistence.demo --scenario crash-after-bank
uv run python -m persistence.demo --scenario wrong-state
```

CI (`.github/workflows/tests.yml`) runs on every push across Linux and Windows. The project does not ship an LLM; users provide their own model or API access. The current live `simulation/tools/test_ollama.py` check specifically tests local Ollama (`ollama pull llama3.2`) and skips when that endpoint is unavailable. The integrated simulation currently permits loopback Ollama; external-provider integration is deferred.

Add a test with every change: a `test_*` function next to the code it checks (`data/`, `sim/`, `tests/`).

## Scripts

| Script | What it does |
|---|---|
| `scripts/test_consume_plane.sh` | Runs the consume-plane tests (`tests/consume_plane`) |
| `scripts/run_consume_plane.sh [events.jsonl [contracts.jsonl]]` | Replays a recorded run through the consume plane and prints the findings; e.g. `tests/consume_plane/fixtures/risky_onboarding.jsonl` |
| `scripts/test_opencode_adapter.sh [--quiet]` | Runs the OpenCode adapter Node tests and prints every JSON request the adapter sends |
| `scripts/check_opencode_pipeline.sh` | Starts real OpenCode with the adapter and passes when its handshake reaches Python (no prompt is sent) |
| `scripts/run_intercept_receiver.sh` | Terminal 1: observe-only Python receiver that logs every adapter request |
| `scripts/run_opencode_intercepted.sh` | Terminal 2: starts `opencode --standalone`, wired to that receiver |

The receiver allows everything; it is a diagnostic tool, not enforcement. Use synthetic data only: it prints raw prompts and tool arguments.
