# OpenCode Project Agents

These are development and review assistants for the hackathon repository, not agents deployed inside the runtime control layer. Their prompts share `AGENTS.md` (automatically discovered by OpenCode) and `docs/project-direction.md` (loaded through `instructions` in `opencode.json`). The competition documents remain separate authoritative sources.

## Agents and Modes

| Agent | Mode | Purpose |
| --- | --- | --- |
| `control-builder` | `primary` | Default implementation lead; owns edits, approved command execution, tests, and specialist delegation. |
| `control-architect` | `all` | Read-only architecture/MVP planning; selectable as a primary agent or callable as a subagent. |
| `rules-auditor` | `subagent` | Competition rules, technical deliverables, judging requirements, and submission evidence. |
| `direction-auditor` | `subagent` | Alignment of code/design/descriptions/pitches with the team's product thesis. |
| `security-auditor` | `subagent` | Trust boundaries, injection, leakage, policy/approval bypass, and budget enforcement. |
| `verification-auditor` | `subagent` | Independent external-state postconditions, uniqueness, false success, and test gaps. |

The builder is primary because implementation needs an ongoing conversation and ownership of changes. The architect uses `all` because interactive design and delegated design reviews are both useful. Auditors are subagents because their work is bounded, read-only, and produces a report rather than taking over implementation. They remain visible in `@` autocomplete.

The builder's Task permission explicitly permits only these specialists, not unrestricted delegation to built-in agents with broader permissions. Auditors deny tools by default and permit repository reads/searches; they cannot edit, run shell commands, invoke other agents, or use unlisted MCP tools. Security/architecture web fetches require approval. The builder requires approval for shell commands and unlisted tools. Permissions are workflow controls, not a complete sandbox: search can still encounter sensitive content, and approved shell execution can access data outside a read-tool restriction. Keep real credentials out of the repository and use synthetic fixtures. OpenCode's built-in agents remain available when explicitly selected; these restrictions describe the custom agents, not every possible OpenCode session.

## NVIDIA Model and Credentials

All six agents and the project default/small model use:

```text
Display: NVIDIA / DeepSeek V4.1 Flash
OpenCode: nvidia/deepseek-ai/deepseek-v4.1-flash
NVIDIA API model ID: deepseek-ai/deepseek-v4.1-flash
```

The exact ID was checked on 2026-10-03 using both `opencode models nvidia` and NVIDIA's live `GET https://integrate.api.nvidia.com/v1/models` catalog. The dot in `v4.1` matters. No custom provider or invented model alias is needed: OpenCode has a built-in NVIDIA provider. Catalog presence does not prove your account has inference access or sufficient quota.

1. Obtain an API key from [NVIDIA Build](https://build.nvidia.com/).
2. In OpenCode, run `/connect`, select NVIDIA, and enter the key. OpenCode stores it outside the project; do not commit it.
3. Alternatively, export `NVIDIA_API_KEY` securely in the environment that launches OpenCode. A repository `.env` is not required.
4. Check `/models` or `opencode models nvidia` lists `nvidia/deepseek-ai/deepseek-v4.1-flash`. If your catalog is stale, run `opencode models nvidia --refresh` and check again.
5. Quit and restart OpenCode after adding or changing configuration, agents, or commands. The running session retains its already-loaded setup.

The project config intentionally sets `default_agent`, `model`, `small_model`, and disables session sharing. It does not disable other providers or replace your global configuration. Project settings merge with global settings; inspect local overrides if behavior differs. NVIDIA inference receives the context sent to it, so disabled sharing is not a data-residency guarantee. The same model is used consistently to start with; no unsupported ranking of alternative models or provider-specific sampling settings is assumed.

## Usage

Select `control-builder` or `control-architect` with Tab. You can invoke a subagent directly:

```text
@rules-auditor Check our current deliverables against both challenge documents.
@direction-auditor Review README.md for accurate project-description alignment.
@security-auditor Review the payment approval and execution boundary.
@verification-auditor Check duplicate booking and false-success coverage.
```

Convenience commands:

```text
/review-project
/review-rules
/review-direction README.md
/review-security path/to/runtime
/review-outcomes path/to/verifier
```

Arguments are optional scopes or questions. The four specialist commands explicitly run as subtasks, keeping their work in child sessions. `/review-project` runs the architect in the main session so it can orchestrate the four auditors; it is intentionally not a subtask because OpenCode's default nesting depth prevents subagents from spawning further subagents. A delegated architect should perform its own bounded design review rather than rely on nested delegation.

Use ordinary paths/questions as command arguments. OpenCode preprocesses shell-injection syntax and `@file` references in command templates, including expanded arguments, before reviewer tool execution. Do not pass shell-injection syntax or sensitive file references as scope text. Reviewer permissions do not sanitize command preprocessing or user-supplied attachments.

The reviewers do not run `git diff` or tests because shell access is denied. Supply sanitized diffs or test results through the builder when needed; reviewers inspect files and report evidence gaps. The builder implements fixes and runs approved validation commands. Commands and prompts encourage review, but they are not automatic pre-commit hooks or mandatory enforcement gates.

## Setup Checks

```sh
opencode --version
opencode agent list
opencode debug agent control-builder
opencode debug agent rules-auditor
opencode models nvidia
```

The configuration was prepared for the installed OpenCode 1.18.34 using the published schema and documented Markdown agents/commands. These checks load configuration and list agents/models; they do not test authenticated NVIDIA inference. Do not paste full resolved global configuration into reports: provider options can contain credentials.

An automated harness is available with Node.js 22+ and OpenCode installed:

```sh
node .opencode/tests/smoke.mjs
```

It copies only the configuration and instruction documents into a disposable Git fixture, uses temporary HOME/XDG paths and a clean child-process environment, and redirects inference to a scripted loopback-only OpenAI-compatible endpoint. It checks loading, effective modes/permissions, shared instructions, each review command, and builder/architect delegation, including completed Task events linked to distinct child sessions and completed parent responses. It never needs a real API key or sends model requests to NVIDIA. This validates configuration and tool orchestration, not model quality, authenticated NVIDIA access, or product-runtime enforcement. The fixture is removed on normal completion and caught failures; forcibly interrupting the process can leave temporary artifacts. This isolation is not an OS-level filesystem/network sandbox; the harness refuses to run if macOS managed-preference storage is present because that may introduce configuration outside temporary HOME.

## References

- [OpenCode agents and mode semantics](https://opencode.ai/docs/agents/)
- [OpenCode permissions](https://opencode.ai/docs/permissions/)
- [OpenCode commands and subtask behavior](https://opencode.ai/docs/commands/)
- [OpenCode NVIDIA provider and authentication](https://opencode.ai/docs/providers/#nvidia)
- [OpenCode configuration schema](https://opencode.ai/config.json)
- [NVIDIA live model catalog](https://integrate.api.nvidia.com/v1/models)
