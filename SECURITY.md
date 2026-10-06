# Security boundaries

Sentinel Interlock is a research and hackathon demonstration using synthetic banking state.
Its controlled Python tool path enforces admission before dispatch; protection depends on using
the gateway and preserving the trusted operator, policy, Task Contract and process boundaries.

## Implemented safeguards

- Loopback bearer-authenticated gateway with a separate token for trusted run binding.
- Deterministic policy checks, transformed-payload revalidation, conservative budget reservations
  and denial when required approvals or supported pricing are unavailable.
- Authenticated configuration writes, browser-origin checks, bounded request sizes, cross-process
  config locking and separate saved/selected policy revisions.
- Structured metadata allowlists for audit payloads. Plugin exceptions become exception types or
  fixed failure codes; raw exception messages are omitted from manager logs, retries and ledgers.
- Atomic SQLite evidence/outbox writes, conflict rejection, bounded delivery retries and exports,
  scoped read authorization and expiring pagination/export retention holds.
- Isolated synthetic bank copies and controlled agent projects; native filesystem/shell tools are
  removed in gateway-backed OpenCode mode. Session storage directories/logs use private POSIX modes.

## Limits that affect deployment

The agent runtime is cooperative. This is not an OS/container escape defense, network isolation
system, general MCP proxy, or a guarantee that arbitrary external actions cannot bypass the gateway.
Text signature controls do not prove prevention of all prompt injections or data leaks. OpenCode
enforcement scans normalized full context and system text; elided oversized content is denied.
Unsupported media is denied in enforcement mode; private reasoning is excluded from inspection. Final answers are not
universally intercepted through the OpenCode adapter, and synthetic hook tests do not establish all
real runtime hook ordering or exception propagation.

SQLite is not tamper-proof against a filesystem owner. Receipts, hashes and immutable-retry checks
bind evidence within the trusted runtime; they are not external signatures or independent notarization.
Delivery is at least once. Verification can detect a wrong effect after execution; it does not roll it back.

Configuration authentication protects policy mutations. Evidence reads and the public demonstration's
session/test endpoints do not implement a general user authentication or tenant authorization system.
Session IDs are bearer capabilities; treat their URLs as sensitive. Rate limits are process-local and
the hosted agent can consume provider quota. Do not expose real data or unrestricted provider accounts
through this demo. Deploy behind an access gate for a private demonstration.

Plugins and backend callbacks are trusted code. `needs_content` is a capability for trusted plugins,
not a sandbox against a malicious plugin; the synthetic runtime uses a separate store per session.
Stored bodies must already be redacted by the trusted caller. Content bytes are bounded and counted
together with event bytes, but comprehensive retention of content and newer governance tables, and
automatic maintenance scheduling, remain future work. Storage must be monitored and maintained.

Child processes need provider credentials; the current environment forwarding is broader than a
dedicated credential allowlist. `.env` and authentication storage must stay private and out of Git.
Terraform's demo SSH ingress defaults to all IPv4 sources; restrict it to an owner-controlled access
path before use. Container base images and the CLI dependency graph are not pinned by digest/lockfile.

## Reporting a vulnerability

Use the repository's private vulnerability reporting channel if its maintainers have enabled one.
Otherwise contact a maintainer privately before disclosing sensitive exploit details. A response
SLA or production security support is not offered by this repository.

Share a minimal synthetic reproduction, affected revision, expected boundary, actual behavior and
relevant test output. Do not attach credentials, customer data, raw provider diagnostics or private logs.
