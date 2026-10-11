# Plan: agent tools (`simulation/tools/`)

The sixteen tools from [use-cases.md](../../use-cases.md): ten KYC tools plus six bait tools. They are the
**monitored system**, not the control layer. They are deliberately naive: no policy, no dedupe, no
permission checks. Every control lives in the gateway, so the gateway is what the tests demonstrate.

| Plan | Scope | Depends on | Estimate |
|---|---|---|---|
| [01-registry.md](01-registry.md) | Registry, context, argument validation, audit logging | – | 1 h |
| [02-kyc-read-tools.md](02-kyc-read-tools.md) | `read_application`, `read_documents`, `extract_fields`, `check_registry`, `screen_sanctions`, `compute_risk` | 01 | 2–3 h |
| [03-kyc-decision-tools.md](03-kyc-decision-tools.md) | `create_client`, `request_more_docs`, `escalate_edd`, `reject_application` | 01 | 1 h |
| [04-bait-tools.md](04-bait-tools.md) | `send_email`, `fetch_url`, `run_code`, `load_risk_model`, `read_config`, `delete_client` | 01 | 1 h |

01 first (one person). After it lands, 02, 03 and 04 can be built in parallel by three people.

## Layout
```
simulation/tools/
  registry.py      # Tool, Ctx, REGISTRY, call(), openai_tools()
  kyc.py           # the ten KYC tools
  bait.py          # the six bait tools
  test_tools.py    # one runnable check, stdlib + optional pytest
```
Four files. No package-per-tool, no base classes, no plugin loader.

## Ground rules (apply to every plan)
- **Python ≥ 3.11, stdlib only** (`sqlite3`, `json`, `re`, `pathlib`, `hashlib`). Same as `data/`.
- **Reuse, don't copy:** `screen`, `norm`, `MATCH_THRESHOLD` from `data/rules.py`; `iban` from
  `data/generate.py`. `registry.py` adds `data/` to `sys.path` once.
- **Identity comes from `Ctx`**, built by the gateway from the authenticated request. No tool takes
  `agent` or `session_id` as an argument the model can fill in.
- **One audit row per executed call** in `audit_actions` (`ts, session_id, agent, tool, args_json,
  result_json`). A call the gateway blocks never reaches `call()`, so it writes no row. That is how
  tests prove "executed: no".
- **Argument names are a contract with the verifier** (`data/postconditions.py`): `screen_sanctions`
  logs `name` and `dob`; `create_client` logs `app_id` and `fields`. Do not rename.
- **"Today" is 2026-10-03** (the dataset's fixed date). `Ctx.now` defaults to it so expiry checks
  and `decided_at` line up with the data.
- **Tools return JSON-serializable dicts.** Errors are `{"error": "..."}` results, still audited.
- **Tools never read `ground_truth.json`.** Only the verifier and tests may.

## Out of scope here
- Policy decisions, redaction, budgets, signature feed: gateway (`proxy/`).
- The HTTP endpoint that exposes the registry (`POST /tools/{name}`): gateway; it calls `registry.call()`.
- Scripted faults (`skip_step`, `swap_arg`, …): agent wrapper (`simulation/agents/`).
- The LLM agent loop: uses `registry.openai_tools()` for its tool list, nothing more.

## Definition of done
1. `uv run python data/generate.py && uv run python tests/support/test_tools.py` passes on a clean checkout.
2. Every tool has at least one happy-path assertion and writes exactly one audit row per call.
3. End-to-end check: run the APP-0001 pipeline through `registry.call()`, then
   `verify_onboarding(con, "APP-0001", calls_from_audit(con, session))` returns no failures; the
   same with `screen_sanctions` skipped fails ONB-P1. This proves the tools and verifier agree.
4. `openai_tools()` output is accepted by the LLM provider's tool-calling API (one manual call).
5. The concurrency test below passes.

## Open questions
- **PII in `audit_actions.result_json`**: read tools return raw OCR text with PESEL numbers. Plan 01
  stores a hash + length for tools marked `log_result=False` instead of the text. Confirm with
  the gateway owner, who may store redacted payloads elsewhere.

## Concurrency (decided)
Several judges hit the same `bank.db` at once through the gateway. SQLite handles this if we:
1. **WAL mode, set once by `data/generate.py`** (it persists in the file, and copies inherit it).
   Do **not** run `PRAGMA journal_mode=WAL` per connection: under contention that statement itself
   fails with `database is locked` without waiting (seen in testing).
2. **`sqlite3.connect(db, timeout=5, isolation_level=None)`**: a waiting caller retries for up to 5 s
   instead of failing.
3. **One connection per `call()`**, opened and closed inside it. No connection is shared across threads.
4. **Every call starts with `BEGIN IMMEDIATE`**, reads included. Every call writes an audit row, and
   a transaction that starts as a read and later writes fails *immediately* (SQLite cannot
   upgrade a stale read snapshot, and the timeout does not help) if another writer committed in
   between. `IMMEDIATE` takes the write lock up front, so calls queue instead of failing.
   `create_client` reads `MAX(client_id)` / `MAX(account_id)` inside that transaction, so parallel
   calls cannot get the same `CLI-NNNN`.

5. **One process-wide `threading.Lock` around the transaction** in `call()` (the gateway is one
   process): calls queue on the lock instead of in SQLite's sleeping busy-wait. The 5 s timeout
   still covers other processes (generator, a second gateway).
6. **`PRAGMA synchronous=NORMAL`** per connection (unlike `journal_mode`, it needs no lock): in WAL
   mode this skips the disk sync on every commit and is still corruption-safe; a power cut can
   lose only the last transaction.

History: steps 1-4 alone measured p99 0.9-2 s locally and **failed in CI on the slower Windows
runner** (`database is locked` after the 5 s timeout). With 5 and 6, the same load (8 threads x 25
create + 4 threads x 100 read) runs in 0.6-1 s total: **0 errors, p50 ~5.5 ms, p99 35-50 ms**.

`test_tools.py` repeats that measurement against the real tools (plan 01).
