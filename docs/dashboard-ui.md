# Dashboard UI

The dashboard is the judges' entry point. They get **2–3 minutes** with it, on **their own laptops**,
without us driving. It has to make them want to try breaking it, and leave them sure it works.

Status: design agreed, not implemented. Open questions are at the end.

---

## Goals by time

| Time | Judge should feel | On screen |
|---|---|---|
| 0–10 s | "This is serious and it works" | Latest test run result, active policy mode, live status |
| 10–90 s | "I tried to break it and it caught me" | Attack console: the judge's own prompt goes through the control layer |
| 90–180 s | "This is smarter than other teams" | Trajectory / outcome demo: every step looked fine, the result was wrong, we caught it |

Anything that does not serve one of these beats stays off the main view.

---

## Visual identity: audit ledger

Domain is banking compliance (KYC, AML), so the UI looks like a printed audit report brought to life.

- Off-white paper background, black ink, thin hairline rules, no cards with shadows.
- Monospace for numbers, IDs, latencies and payloads; a plain sans for prose.
- Color carries meaning only: **red = BLOCK**, **amber = REDACT / ESCALATE**, **muted green = ALLOW**. Nothing else is colored.
- Verdicts are shown as **rubber stamps**: `BLOCKED`, `REDACTED`, `ESCALATED TO HUMAN`, `ALLOWED`.
- Light theme only (bright arena, varied laptop screens).

### Explicitly avoid (generic AI dashboard look)
Dark navy with purple/blue gradients, glassmorphism, rows of KPI tiles with sparklines, emoji icons,
donut charts of fake traffic, world maps, a chatbot bubble.

---

## Layout

```
┌──────────────────────────────────────────────────────────────┐
│ AI CONTROL LAYER   policy v12 · STRICT ▾   ● live   [QR]    │
├──────────────────────────────────────────────────────────────┤
│ TEST SUITE 27/28 · 0 false positives · 4 min ago · a1b2c3   │
│ ▮▮▮▮▮▮▮ PII  ▮▮▮▮ INJ  ▮▮▮ BUDGET  ▮▮▮▮▯ TRAJ  ▮▮▮▮▮ OUTCOME│
├───────────────────────────────┬──────────────────────────────┤
│ 1. TRY TO BREAK IT            │ 2. AGENT SAID DONE. WAS IT?  │
│ [presets] [textarea] [Send]   │ scenario replay timeline     │
│ pipeline rows light up        │ step ✓ step ✓ step ✓ BLOCKED │
│ sanitized view + STAMP        │ evidence: rule still fires   │
├───────────────────────────────┴──────────────────────────────┤
│ LIVE FEED · Stump the guard: 14 tried / 13 caught            │
└──────────────────────────────────────────────────────────────┘
```

Must work at 13" laptop width and degrade to a single column on a phone.

---

## Panels

### Header
- Product name, active **policy version**, **strictness selector** (lenient / standard / strict), live/offline indicator.
- QR code + short URL so other judges can join from their own device.

### Test suite strip
- Headline: `passed/total`, time since run, commit hash.
- One small cell per test case, grouped by control family (PII, secrets, injection, budget, exploit signatures,
  trajectory, outcome). Green pass, red fail. Click/tap shows case ID (e.g. `ONB-09`), expected vs actual.
- **Positive cases reported separately**: "0 false positives on N legitimate requests". Over-blocking is the
  first thing a security judge looks for.
- The exact command to run the suite themselves.

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
  04 injection (local LLM)    BLOCK   180 ms     score 0.94
  05 budget                   —       skipped
  ```
  This shows hybrid deterministic + semantic controls, architecture and performance telemetry in one view.
- Deterministic results show **instantly**; the local-LLM check streams in after. The delay should read as
  "pipeline working", not lag.
- **What the model actually saw**: original vs sanitized prompt, redactions as black bars labelled `[PESEL]`, `[IBAN]`.
- Final verdict stamp, plus model response if allowed.
- **Strictness toggle demo**: re-send the same prompt under another policy level and watch the verdict change
  (shows the live-reloaded central policy).

### 2. Agent said done. Was it? (trajectory / outcome)
Our differentiator. Single-message filters are what every team builds; this shows a whole agent run.

Example, `TXM-03` (see [use-cases.md](use-cases.md)): the future AML agent closes real structuring alert `ALR-0002` as a
false positive and reports success. In the intended scenario, each tool call passes its per-call authorization checks. The outcome verifier re-runs
the rule on ground truth, it still fires, postcondition `TXM-P1` fails, and the close is blocked. **AML is
deferred in the current KYC-only scope**; use a KYC scenario such as `ONB-07` for the initial live demo unless
the team explicitly reopens AML scope.

- Judge clicks **Run scenario**; steps appear one by one on a timeline, each with its own check.
- Final step stamped `BLOCKED — outcome mismatch` with the evidence.
- Headline on screen: **"Agent said success. We checked. It wasn't."**
- Can be a **recorded run replayed** (verifier runs live on the recorded events), labelled as such.
  Live agent run is optional.
- Candidate scenarios: `TXM-03`, `ONB-07`, `TXM-08` (drift: browsing unrelated customers).

### Live feed + "Stump the guard"
- Scrolling tape of recent decisions across **all** judges (shared server), newest first: time, verdict, rule, short excerpt.
- Counter: `Judge attempts today: N · caught: M`.
- If something gets through, we show it and offer **"Add as regression test"**, which turns the bypass into a new
  negative case in the self-testing suite. Honest about misses, closes the loop.

---

## Practical constraints

- **Zero-friction access**: one URL + QR, no install, no login.
- **Works on bad Wi-Fi**: no CDN fonts or libraries; everything bundled/inline.
- **Concurrent judges**: Ollama queues requests; deterministic checks must never wait on the LLM.
- **Self-guiding**: one-line explanation per panel, presets as "Try this:". No manual.
- **Hostile input**: render all payloads as plain text (never HTML), cap input size; a giant paste should hit the
  budget control and show it working.
- **Honesty**: anything mocked, recorded or sample data is labelled. Never show fake numbers as live.
- **Warm-up**: load the Ollama model before judging starts.

---

## Open questions

1. **Trajectory replay feasibility**: can the backend deliver at least one recorded scenario + live verifier in 24 h?
   Reserve the panel in the layout either way.
2. **Hosting**: deployed URL (VPS or tunnel to our laptop) vs our laptop on venue Wi-Fi. Venue networks often
   isolate clients; **test at the venue early**. Last resort: judges use our laptop.
3. **API contract** between dashboard and gateway (test results, playground request, event feed, policy switch),
   so frontend and backend can work in parallel.
