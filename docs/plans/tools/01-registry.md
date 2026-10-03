# 01: Registry, context and audit (`sim/tools/registry.py`)

Owner: one person, first. Everything else imports this.

## What it provides
```python
@dataclass
class Ctx:                      # built by the gateway per request, never by the model
    agent: str                  # "onboarding-agent" | "admin-agent"
    session_id: str
    db: Path = BANK_DB          # env BANK_DB, default data/bank.db
    now: datetime = TODAY       # 2026-10-03T12:00:00+02:00

@dataclass
class Tool:
    name: str
    fn: Callable[[sqlite3.Connection, Ctx, ...], dict]
    args: dict[str, tuple[type, bool]]   # name -> (type, required)
    side_effect: str                     # "read" | "write" | "irreversible"
    description: str                     # shown to the LLM
    log_result: bool = True              # False: audit stores {"sha256", "chars"} instead of the result
    bait: bool = False                   # labelled as fake in UI / README

REGISTRY: dict[str, Tool]
def tool(name, args, side_effect, description, log_result=True, bait=False): ...  # decorator, fills REGISTRY
def call(name: str, args: dict, ctx: Ctx) -> dict: ...
def openai_tools(names: list[str] | None = None) -> list[dict]: ...
```
`kyc.py` and `bait.py` register themselves with `@tool(...)`; `registry.py` imports both at the
bottom so `REGISTRY` is complete after `import registry`.

## `call()` steps
1. Unknown tool → `{"error": "unknown tool"}`, **no audit row** (nothing executed).
2. Validate args: required present, types match (`str`, `int`, `float`, `bool`, `dict`, `list`), **no
   extra keys**. Failure → `{"error": "..."}`, no audit row. This rejects `agent`/`session_id`
   smuggled in by the model.
3. Open a fresh connection for this call only (see [Concurrency](README.md#concurrency-decided)):
   ```python
   con = sqlite3.connect(ctx.db, timeout=5, isolation_level=None)   # explicit transactions
   con.execute("BEGIN IMMEDIATE")   # for every tool, reads included: each call writes an audit row
   ```
   No per-connection `journal_mode` pragma (the DB is created in WAL mode by the generator), and no
   plain `BEGIN` for read tools: both fail under contention without waiting. Reasons in the README.
   Then run `fn(con, ctx, **args)`. A raised `ToolError(msg)`
   becomes `{"error": msg}`; any other exception propagates (bug, not a tool result).
4. Insert into `audit_actions (ts, session_id, agent, tool, args_json, result_json)` in the **same
   transaction** as the tool's own writes, then `COMMIT`. Either both persist or neither does.
   Close the connection in a `finally`.
5. Return the result.

`args_json` is the args exactly as executed (the gateway passes already-redacted args for BAIT-03).
`json.dumps(..., ensure_ascii=False, sort_keys=True)` for stable output.

## `openai_tools()`
Maps each `Tool` to the OpenAI function-calling schema (`{"type": "function", "function": {name,
description, parameters: {type: object, properties, required, additionalProperties: false}}}`).
`names` filters to an agent's allowlist; the gateway decides the allowlist, not this module.

## Check (in `test_tools.py`)
- Build a fresh dataset in a temp dir (`generate.build(tmp)`, as `data/test_postconditions.py` does).
- Unknown tool and an extra `agent` argument: error, zero audit rows.
- A dummy read call writes exactly one row with the right `agent` and `session_id`.
- A tool that raises `ToolError` after writing: its write is rolled back, the audit row with the
  error is kept. Implementation: on `ToolError`, `rollback()`, then insert the audit row and `commit()`.
- `openai_tools()` round-trips through `json.dumps`, and every schema has `additionalProperties: false`.
- **Concurrency**: 8 threads × 25 `create_client` calls (plan 03) on one fresh DB, each thread with
  its own `Ctx`. No exception, 200 distinct `client_id`s, 200 distinct `account_id`s, 200 audit rows.
  Add 4 reader threads looping `read_application` meanwhile; none may fail. Until plan 03 lands,
  run it with a dummy write tool that inserts `MAX(id)+1` into a scratch table.
