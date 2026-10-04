# AI Control Layer

A policy-driven runtime control and verification layer for autonomous AI agents.

The system is intended to sit between agents and LLMs, MCP servers, APIs, tools, and other agents. It combines deterministic interaction enforcement, task-level trajectory supervision, and independent verification of the resulting external state under a shared Task Contract and centralized policy.

```text
Is the action authorized?       -> deterministic policy
Does it serve the task?         -> trajectory supervision
Did the correct result exist?  -> independent outcome verification
```

### Current Implementation State
- **Implemented so far:** The synthetic banking dataset generator (`data/generate.py`, `data/rules.py`, `data/report.py`), the KYC outcome verifier (`data/postconditions.py`), and the 16 agent tools (`simulation/tools/`, of which 6 are **bait tools: fakes** that only exist so the control layer has something to intercept/block).
- **Interception Layer:** Initial Python asyncio interception service with configurable allowlist, signature-scanner, and webhook auditors (`intercept/`).
- **OpenCode Adapter:** JavaScript plugin for OpenCode v2.0.22 (`adapters/opencode/`) that forwards tool calls (always) and prompts/model requests (opt-in) to the Python service. Real OpenCode loads it, which the startup handshake verifies. `intercept/service/receiver.py` is an observe-only Python server for manual checks: it logs and allows everything. See [OpenCode forwarding](docs/intercept/opencode-forwarding.md).
- **Integrated KYC runtime:** simulation tool proposals now pass through Layer 1, durable Layer 2 evidence/outbox and the existing ConsumerManager with trajectory-risk feedback and lifecycle outcome verification. Both drivers share the governed path. See [local execution and limits](docs/integrated-runtime.md).
- **Durable Persistence:** `persistence/` supplies sanitized immutable SQLite evidence, atomic outbox commits, bounded consumer delivery with DLQ, run binding and contiguous action indexing, atomic banking receipts and replication, scoped readers with keyset pagination, and maintenance/backup facilities. See [integration and limits](docs/persistence.md).
- **Architecture & Runtime Scope:** The MVP scope is **KYC only**; AML is deferred.

## Project Layout

One agent action flows through three planes; [details](docs/agent-action-model.md).

```text
OpenCode agent ─► adapters/opencode ─► intercept (L1) ─► persistence (L2) ─► consume_plane (L3)
                  plugin hooks         allow/block        evidence + outbox    risk, findings, verification
                                            ▲                                        │
                                            └────────── tighten-only feedback ───────┘
```

| Path | Role |
|---|---|
| `contracts/` | Shared models: `ActionProposal`, `AgentAction`, decisions, `TaskContract`, feedback signal, v2.1 decoder |
| `adapters/opencode/` | OpenCode 2.0.22 plugin: forwards tool calls and prompts to Layer 1 |
| `intercept/` | Layer 1. `policy/` rules and auditors · `governed/` gateway, prompts, baseline · `service/` HTTP server, receiver · `tools/`, `cli/` |
| `persistence/` | Layer 2. SQLite evidence store, outbox delivery, `events.py` (Layer 1 → storage), `adapters/` (storage → v2.1) |
| `consume_plane/` | Layer 3. Async plugin runtime; built-in plugins in `consume_plane/plugins/`, drop-in ones in `plugins/` |
| `simulation/` | Governed KYC agent (scripted or LLM driver), synthetic tools, central `policy.json` |
| `data/` | Synthetic bank generator, rules, outcome postconditions |
| `tracing/` | Pipeline trace logger: terminal, file (JSON lines) or null |
| `scripts/` | Run and test entry points (below) |
| `tests/`, `*/test_*.py` | Python tests; `adapters/opencode/*.test.mjs` for the plugin |
| `docs/` | Design docs, starting with [project direction](docs/project-direction.md) |

## Tracing

Every agent action can be traced through the pipeline: interception and decision (`intercept`), the
durable commit with its `seq` (`persistence`), then consumer processing, findings, feedback and
verification (`consume`). Select the logger with `CONTROL_LOG=terminal|file|null` (default `null`; `file`
writes JSON lines to `CONTROL_LOG_FILE`, default `var/control-layer.log`). `scripts/run_demo.sh` traces to
the terminal by default. Only IDs, names and reason codes are logged, never prompts or tool arguments.
The interface and its three implementations are in `tracing/`.

## Documentation Index

- [Integrated local KYC execution](docs/integrated-runtime.md)
- [Project direction](docs/project-direction.md)
- [Agent action model and three-plane pipeline (code layout, single sources)](docs/agent-action-model.md)
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
- [Deployment plan](docs/dashboard/deployment.md)
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

Quick start: `scripts/test.sh` runs every test, and `scripts/run_demo.sh` runs the control layer end to end
offline. The underlying commands:

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

CI (`.github/workflows/tests.yml`) runs on every push across Linux and Windows. The project uses paid API models (team decision; keys from env vars, never committed). The code has not moved yet: the LLM driver and the live `simulation/tools/test_ollama.py` check still target a loopback Ollama endpoint and skip when it is unavailable. Switching them to the paid API is pending.

Add a test with every change: a `test_*` function next to the code it checks (`data/`, `simulation/`, `intercept/`), or under `tests/`.

## Deployment

Full plan, cost and secret handling: [docs/dashboard/deployment.md](docs/dashboard/deployment.md).

- The app runs on one AWS EC2 instance under Docker Compose, behind one public HTTPS URL.
- Pushing to `main` runs the tests and does **not** deploy.
- A push to the `deploy` branch deploys. `deploy` is a pointer to what is live; nobody commits to it directly.

```sh
# Release the current main (pull first)
git checkout main && git pull
git push origin main:deploy

# Roll back to an earlier commit
git push --force origin <good-sha>:deploy
```

Run the same stack locally (needs Docker):

```sh
cp .env.example .env
docker compose up -d --build
# open http://localhost  (health check: http://localhost/healthz)
docker compose down
```

The web app is `web/main.py` (FastAPI) and the frontend files are in `static/`.

The deploy workflow runs the test suite, rebuilds the app container on the instance and checks its
health endpoint. If the check fails, the container logs are printed in the GitHub Actions run.
Nobody on the team needs AWS or instance access to release.

## Scripts

Details and a live-trace walkthrough: [scripts/README.md](scripts/README.md).

| Script | What it does |
|---|---|
| `scripts/run_live_pipeline.sh [APP-0001]` | Full governed pipeline traced to a file, waiting for an interactive OpenCode session (`scripts/run_opencode_intercepted.sh`) |
| `scripts/test.sh [all\|python\|intercept\|persistence\|consume\|e2e\|adapter\|data] [pytest args]` | Runs all tests, or one layer's |
| `scripts/run_demo.sh [APP-ID] [--fault F]` | Runs the whole control layer offline (scripted agent, no model) and prints the verdict |
| `scripts/test_consume_plane.sh` | Runs the consume-plane tests (`tests/consume_plane`) |
| `scripts/run_consume_plane.sh [events.jsonl [contracts.jsonl]]` | Replays a recorded run through the consume plane and prints the findings; e.g. `tests/consume_plane/fixtures/risky_onboarding.jsonl` |
| `scripts/test_opencode_adapter.sh [--quiet]` | Runs the OpenCode adapter Node tests and prints every JSON request the adapter sends |
| `scripts/check_opencode_pipeline.sh` | Starts real OpenCode with the adapter and passes when its handshake reaches Python (no prompt is sent) |
| `scripts/run_intercept_receiver.sh` | Terminal 1: observe-only Python receiver that logs every adapter request |
| `scripts/run_opencode_intercepted.sh` | Terminal 2: starts `opencode --standalone`, wired to that receiver |
| `scripts/setup_opencode_pipeline.sh` | Installs the locked environment and pinned OpenCode 2.0.22 under ignored `var/opencode-cli` |
| `scripts/run_pipeline.sh [APP-0001] --model provider/model` | Runs one synthetic application through the governed gateway, OpenCode, and outcome verification |

The receiver allows everything; it is a diagnostic tool, not enforcement. Use synthetic data only: it prints raw prompts and tool arguments.
