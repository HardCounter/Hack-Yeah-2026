# AI Control Layer

A policy-driven runtime control and verification layer for autonomous AI agents.

The system is intended to sit between agents and LLMs, MCP servers, APIs, tools, and other agents. It combines deterministic interaction enforcement, task-level trajectory supervision, and independent verification of the resulting external state under a shared Task Contract and centralized policy.

```text
Is the action authorized?       -> deterministic policy
Does it serve the task?         -> trajectory supervision
Did the correct result exist?  -> independent outcome verification
```

Implemented so far: the mock banking dataset (`data/`), the KYC outcome verifier (`data/postconditions.py`) and the 16 agent tools (`simulation/tools/`, of which 6 are **bait tools: fakes** that only exist so the gateway has something to block). The gateway, agent loop and dashboard are not built yet.

## Run the simulation

```bash
uv sync && uv run python data/generate.py                           # once: install, build data/bank.db
uv run python simulation/agent.py APP-0001                          # llama3.2 on Ollama works one application
uv run python simulation/agent.py APP-0005                          # the poisoned document: does the model fall for it?
uv run python simulation/agent.py APP-0003 --driver scripted --fault skip_step:screen_sanctions
uv run python simulation/agent.py APP-0010 --driver scripted --fault "skip_step:screen_sanctions#4"
```

Each run prints every tool call, the decision, the expected decision from the answer key (read by the
harness after the run, never by the agent) and the outcome verifier's verdict (exit code 1 on BLOCK).
`--driver scripted` is a deterministic by-the-book analyst; faults (`skip_step`, `swap_arg`, `repeat`,
`loop`, `extra_call`) come from [use-cases.md](docs/use-cases.md). Runs use their own DB copy in
`data/runs/`. Other model: `--model qwen2.5:7b` (llama3.2 is small and makes mistakes, which is the point,
but it also gets lost). Not wired to the `intercept/` gateway yet.

## Tests

```bash
uv sync                          # Python + dev dependencies (pytest) from uv.lock
uv run python data/generate.py   # build the dataset (self-checks + determinism)
uv run pytest                    # whole suite
```

CI (`.github/workflows/tests.yml`) runs the same on every push, on Linux and Windows. A red build means
something broke; fix it before building on top. `simulation/tools/test_ollama.py` talks to a local model
(`ollama pull llama3.2`) and skips itself when Ollama is not running, as in CI.

Add a test with every change: a `test_*` function next to the code it checks (`data/`, `simulation/`, `intercept/`).
This repository contains competition documents, project direction, development/review configuration, simulated banking tools, and an initial Python asyncio interception service with an opt-in OpenCode V2 JavaScript adapter. The initial interception Python tests and JavaScript callback-harness tests pass; live OpenCode enforcement and complete architecture coverage are not demonstrated. See the [implementation status and limitations](docs/intercept/implementation-status.md).

- [Project direction](docs/project-direction.md)
- [Implementation stack: Python asyncio and uv](docs/stack.md)
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
