# Dashboard UI

The dashboard is the judges' entry point. We target **2–3 minutes** of self-guided exploration,
ideally on their own laptops. This is a design target, not an organizer requirement.

Status: design agreed, not implemented. Open questions are at the end.

MVP: KYC only; AML is deferred. Follow [architecture-contract.md](architecture-contract.md)
for trusted sessions, policy pinning, pre-action gates and independent persisted-state checks.
All numbers and latency figures below are layout examples, not measured results.

---

## Goals by time

| Time | Judge should feel | On screen |
|---|---|---|
| 0–10 s | "This is serious and it works" | Latest test run result, active policy mode, live status |
| 10–90 s | "I tried to break it and it caught me" | Attack console: the judge's own prompt goes through the control layer |
| 90–180 s | "This is smarter than other teams" | Trajectory / outcome demo: every step looked fine, the result was wrong, we caught it |

Anything that does not serve one of these beats stays off the main view.

---


## Layout

```
┌──────────────────────────────────────────────────────────────┐
│ AI CONTROL LAYER   policy v12 · STRICT ▾   ● live   [QR]    │
├──────────────────────────────────────────────────────────────┤
│ TEST SUITE not run · no runtime tests implemented yet       │
│ ▮▮▮▮▮▮▮ PII  ▮▮▮▮ INJ  ▮▮▮ BUDGET  ▮▮▮▮▯ TRAJ  ▮▮▮▮▮ OUTCOME│
├───────────────────────────────┬──────────────────────────────┤
│ 1. TRY TO BREAK IT            │ 2. AGENT SAID DONE. WAS IT?  │
│ [presets] [textarea] [Send]   │ scenario replay timeline     │
│ pipeline rows light up        │ persisted result: FAILED_POST│
│ sanitized view + STAMP        │ evidence: rule still fires   │
├───────────────────────────────┴──────────────────────────────┤
│ LIVE FEED · Stump the guard: 14 tried / 13 caught            │
└──────────────────────────────────────────────────────────────┘
```

Must work at 14" or 16" laptop width


---

## Panels

### Header
- Product name, bound **policy/feed versions**, live/offline indicator. An authenticated
  admin selector publishes modes for new sessions; public users cannot mutate global
  policy or weaken active contracts. Show the session's pinned version separately.
- QR code + short URL so other judges can join from their own device.

### Test suite strip
- Headline: actual `passed/total`, command, time since run, commit hash and policy/feed versions.
  Until tests execute show `not run`, never infer totals/passes from planned scenario tables.
- One small cell per test case, grouped by control family (PII, secrets, injection, budget, exploit signatures,
  trajectory, outcome). Green pass, red fail. Click/tap shows case ID (e.g. `ONB-09`), expected vs actual.
- **Positive cases reported separately**: "0 false positives on N legitimate requests". Over-blocking is the
  first thing a security judge looks for.
- The exact **uv** command to run the suite once implemented. No runnable product-suite
  command exists yet; do not advertise `uv run pytest` as currently working.

### 1. Try to break it (attack console)
- Textarea + **Send**. Ctrl+Enter submits.
- One-click presets so nobody faces an empty box:
  injection hidden in a document, DAN-style jailbreak, PESEL + IBAN, leaked API key,
  dangerous tool request ("freeze all accounts"), and one **benign request that must pass**.
- **Pipeline trace**: each control appears in order with its result and latency, e.g.
  ```
  01 auth / identity          pass      0.2 ms
  02 secrets regex            pass      0.4 ms
  03 PII detector             REDACT    0.6 ms   PESEL, IBAN
  04 budget reservation       pass      0.3 ms
  05 selective semantic gate  BLOCK   180 ms     score 0.94
  ```
  This shows hybrid deterministic + semantic controls, architecture and performance telemetry in one view.
- Deterministic results appear first; required semantic assessment finishes **before** dispatch.
  Progress can stream to the UI, but unchecked tool/LLM content cannot. A hard deny skips semantic work.
- **What the model actually saw**: sanitized prompt and labelled redactions. Do not retain
  raw PII/secrets in logs, shared UI, hidden HTML, exceptions or blob links. Protected verifier
  baseline data is separate from dashboard evidence.
- Final verdict stamp, plus model response if allowed.
- **Strictness demo**: an authorized admin publishes another mode; send the same prompt in
  a new sandbox session. Show the changed version/verdict and the original session's pinned version.

### 2. Agent said done. Was it? (trajectory / outcome)
Our differentiator. Single-message filters are what every team builds; this shows a whole agent run.

KYC example: an approved APP-0007 instruction produces a persisted client with the wrong
legal identity and the agent reports success. The deterministic pre-action ONB-07 test
should block a proposed substitution. A separate **corrupted-writer test fixture** (`CW-01`) deliberately
persists incorrect state despite valid submitted terms, proving the independent verifier
reads actual state and reports failed `ONB-P2`. (Earlier AML concept `TXM-03` is deferred in the current KYC-only scope; use KYC scenarios for the initial live demo). Do not disable enforcement through the public UI.

- Judge clicks **Run scenario**; steps appear one by one on a timeline, each with its own check.
- Final step stamped `FAILED POSTCONDITIONS — persisted identity mismatch`, with read-only
  state evidence. Also support `VERIFIED_SUCCESS` and `VERIFICATION_INCOMPLETE`.
  If the wrong effect already committed, say **detected, not prevented or rolled back**.
- Headline on screen: **"Agent said success. We checked. It wasn't."**
- Can be a **recorded simulation replay** with a preserved immutable bank-state snapshot
  and trusted baseline. The verifier queries that snapshot; events alone are not outcome proof.
  Label recorded, synthetic and corrupted-writer fixtures explicitly. Live agent run is optional.
- Candidate KYC scenarios: ONB-07 plus corrupted persisted-state fixture, ONB-11 concurrent
  duplicate attempts, ONB-15 blocked unrelated-application retrieval. AML scenarios are deferred.

### Live feed + "Stump the guard"
- Scrolling tape of recent decisions across **all** judges (shared server), newest first: time, verdict, rule, short sanitized excerpt (e.g. `[REDACTED_SECRET]` or masked data; never raw secrets or PII).
- Counter: `Judge attempts today: N · caught: M`.
- If something gets through, offer **"Propose regression fixture"**. Save sanitized data for
  authorized review; never execute judge input as generated test code or treat it as policy authority.

---

## Practical constraints

- **Public sandbox access**: one URL + QR, no install; isolated synthetic sessions only,
  with quotas/size limits. Separate authenticated admin/approval access. No shared private traces.
- **Works on bad Wi-Fi**: no CDN fonts or libraries; everything bundled/inline.
- **Concurrent judges**: bounded queues, per-session and shared atomic resource reservations.
  Deterministic checks run first; required pre-action semantic gates still wait before dispatch.
- **Self-guiding**: one-line explanation per panel, presets as "Try this:". No manual.
- **Hostile input**: render all payloads as plain text (never HTML), cap input size; a giant paste should hit the
   size limit before allocating model work. Demonstrate token/tool budgets separately.
- **Honesty**: anything mocked, recorded or sample data is labelled. Never show fake numbers as live.
- **Warm-up**: make sure the user-provided LLM (own API key or locally hosted model) is reachable and warmed up before judging starts.

---

## Open questions

1. **Trajectory replay feasibility**: can the backend deliver at least one recorded scenario + live verifier in 24 h?
   Reserve the panel in the layout either way.
2. **Hosting**: deployed URL (VPS or tunnel to our laptop) vs our laptop on venue Wi-Fi. Venue networks often
   isolate clients; **test at the venue early**. Last resort: judges use our laptop.
3. **API contract**: implemented to match the proposed endpoints in [application-documentation.md](application-documentation.md#54-gateway-dashboard-api-contract-for-judge-ui) (`/api/v1/inspect`, `/api/v1/events/stream`, `/api/v1/policy`, `/api/v1/policy/mode`, `/api/v1/approvals/{approval_id}/decide`, `/api/v1/suite/status`, `/api/v1/scenario/replay`).
