# HackYeah 2026

## Hackathon
- HackYeah 2026, Tauron Arena Kraków, 2026-10-03 to 2026-10-04. 24h sprint, goal is to win a partner prize.
- Team: 5 computer-science students (max team size is 6).
- Briefs and rules (PDFs) are in `Docs/`. Read them before suggesting scope changes.
- Priority is a working, demoable system over polish. Keep dependencies and abstractions minimal.

## Task: Goldman Sachs "AI Control Layer"
Build a lightweight gateway / proxy / middleware / SDK wrapper that sits between AI systems (agents, MCP services, LLMs, APIs) and enforces security, privacy and budget controls in real time: inspect, redact or block unsafe interactions.

Core idea (from the brief):
- **Hybrid defense**: deterministic controls (regex for PII/secrets, auth/access checks) plus semantic AI-based controls (e.g. prompt-injection detection with a local model).
- **Centralized policy engine**: one config source (e.g. a YAML file) for controls, thresholds (block vs redact), allowed models and budgets. Judges may edit it live, so changes must apply without a restart.
- **Budget governance**: token/cost/compute limits for both commercial APIs and local models.
- **Historical attack mitigation**: signature feed for known AI exploits (malicious code execution, unsafe deserialization, model-repo supply-chain). Cover OWASP LLM/agentic risks.
- **Reporting**: live dashboard (controls, security posture, blocked threats, cost) plus exportable audit logs. Expose performance telemetry.
- **Self-testing suite**: automated tests with both positive (allowed) and negative (blocked/redacted) cases, including budgets and exploit mitigation. Judges will run it.
- **Deliverables**: working control layer, architecture diagram, documented sample policy file (with different strictness levels), dashboard, runnable test suite.
- No paid APIs are provided. Everything must run locally (e.g. Ollama). Stack is free; check licenses of any open-source base.

## Scoring (pass needs >= 50%)
Robustness and guardrail quality 30% · Architecture and performance 20% · Security reporting 20% · Self-testing suite 15-20% · Practical implementability/scalability 10-15%.
(The rules PDF says 20/10 for the last two, the criteria PDF says 15/15. Treat them as roughly equal.)

## Submission (HackTribe, English or Polish)
- Work started at **11:00 on Oct 3** and must be submitted by **11:00 on Oct 4**. Later changes are ignored.
- Needs: project title, team name, member list, description, and a PDF of at most 10 slides (screenshots, repo, demo links optional).
- Two phases: mentor review on HackTribe, then a live pitch to the jury for finalists.
- Authors keep their copyright.
