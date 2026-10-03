# AI Control Layer

A policy-driven runtime control and verification layer for autonomous AI agents.

The system is intended to sit between agents and LLMs, MCP servers, APIs, tools, and other agents. It combines deterministic interaction enforcement, task-level trajectory supervision, and independent verification of the resulting external state under a shared Task Contract and centralized policy.

```text
Is the action authorized?       -> deterministic policy
Does it serve the task?         -> trajectory supervision
Did the correct result exist?  -> independent outcome verification
```

This repository currently contains the competition documents, recorded project direction, and OpenCode development/review configuration. Runtime implementation and executable product tests are not present yet.

- [Project direction](docs/project-direction.md)
- [Technical challenge and criteria](GoldmanSachsCriteria.md)
- [Competition rules](GoldmanSachsRules.md)
- [OpenCode agents, NVIDIA setup, and review commands](.opencode/README.md)
- [Shared engineering instructions](AGENTS.md)

The Rules and Criteria disagree on self-testing and scalability scoring weights. Preserve both sources and confirm the applicable weights with the organizer.
