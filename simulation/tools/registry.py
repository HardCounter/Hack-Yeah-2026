"""Tool registry for the monitored agents: Ctx, Tool, REGISTRY, call(), openai_tools().

Tools are deliberately naive (no policy); every control lives in the gateway. Each executed call
writes one audit_actions row in the same transaction as the tool's own writes.
See docs/plans/tools/01-registry.md and README.md (Concurrency).
"""
import hashlib
import json
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "data") not in sys.path:
    sys.path.insert(0, str(REPO / "data"))
from rules import CEST  # noqa: E402

TODAY = datetime(2026, 10, 3, 12, 0, tzinfo=CEST)
BANK_DB = Path(os.environ.get("BANK_DB", REPO / "data" / "bank.db"))
TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}


class ToolError(Exception):
    """Raised by a tool for an expected failure: becomes {"error": msg}, writes rolled back, still audited."""


@dataclass
class Ctx:
    agent: str
    session_id: str
    db: Path = BANK_DB
    now: datetime = TODAY


@dataclass
class Tool:
    name: str
    fn: Callable[..., dict]
    args: dict[str, tuple[type, bool]]
    side_effect: str
    description: str
    log_result: bool = True
    bait: bool = False


REGISTRY: dict[str, Tool] = {}
_LOCK = threading.Lock()  # the gateway is one process; SQLite's timeout still covers other processes


def tool(name, args, side_effect, description, log_result=True, bait=False):
    """Decorator: register fn(con, ctx, **args) -> dict under `name`."""
    assert side_effect in ("read", "write", "irreversible"), side_effect
    assert all(t in TYPES for t, _ in args.values()), name

    def reg(fn):
        REGISTRY[name] = Tool(name, fn, args, side_effect, description, log_result, bait)
        return fn
    return reg


def _check(t, args):
    if not isinstance(args, dict):
        return "args must be an object"
    if extra := set(args) - set(t.args):
        return f"unexpected argument(s): {', '.join(sorted(extra))}"
    for k, (typ, required) in t.args.items():
        if k not in args:
            if required:
                return f"missing argument: {k}"
            continue
        v = args[k]
        ok = isinstance(v, typ) and not (isinstance(v, bool) and typ is not bool)
        if typ is float and isinstance(v, int) and not isinstance(v, bool):
            ok = True  # JSON 1 is a valid number
        if not ok:
            return f"argument {k} must be {TYPES[typ]}"


def _dump(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def call(name: str, args: dict, ctx: Ctx, *, before_execute=None, before_commit=None,
         audit_projection=None, gateway_action_id: str | None = None,
         durable: bool = False, after_audit=None) -> dict:
    """Validate, execute and audit one tool call. Unknown tool / bad args: error, nothing executed, no audit row."""
    t = REGISTRY.get(name)
    if t is None:
        return {"error": "unknown tool"}
    if err := _check(t, args):
        return {"error": err}
    con = sqlite3.connect(ctx.db, timeout=5, isolation_level=None)
    con.execute("PRAGMA synchronous=FULL" if durable else "PRAGMA synchronous=NORMAL")
    try:
        with _LOCK:  # calls in this process queue here instead of in SQLite's sleeping busy-wait (README, Concurrency)
            con.execute("BEGIN IMMEDIATE")  # reads too: every call writes an audit row (README, Concurrency)
            try:
                # Trusted gateway callback rechecks persisted state after admission and
                # while holding SQLite's write lock, closing the precheck/dispatch race.
                if before_execute is not None:
                    before_execute(con, ctx, name, args)
                result = t.fn(con, ctx, **args)
                if before_commit is not None and "error" not in result:
                    # Business receipts must share the same transaction as the effect.
                    before_commit(con, ctx, name, args, result)
            except ToolError as e:
                con.rollback()
                con.execute("BEGIN IMMEDIATE")
                result = {"error": str(e)}
            except BaseException:
                con.rollback()
                raise
            logged = result
            audited_args, audited_result = args, result
            if audit_projection is not None:
                audited_args, audited_result = audit_projection(name, args, result)
            if not t.log_result and "error" not in result:
                s = _dump(audited_result)
                logged = {"sha256": hashlib.sha256(s.encode()).hexdigest(), "chars": len(s)}
            else:
                logged = audited_result
            audit_ts = datetime.now(CEST).isoformat(timespec="milliseconds")
            columns = {row[1] for row in con.execute("PRAGMA table_info(audit_actions)")}
            if "action_id" in columns:
                con.execute("INSERT INTO audit_actions (ts, session_id, agent, tool, args_json, result_json, action_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (audit_ts, ctx.session_id, ctx.agent, name,
                             _dump(audited_args), _dump(logged), gateway_action_id))
            else:
                con.execute("INSERT INTO audit_actions (ts, session_id, agent, tool, args_json, result_json) VALUES (?, ?, ?, ?, ?, ?)",
                            (audit_ts, ctx.session_id, ctx.agent, name,
                             _dump(audited_args), _dump(logged)))
            if after_audit is not None:
                after_audit(con, ctx, name, args, result, audit_ts)
            con.commit()
            return result
    finally:
        con.close()


def openai_tools(names: list[str] | None = None) -> list[dict]:
    """OpenAI function-calling schemas, optionally filtered to an allowlist (decided by the gateway)."""
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description,
        "parameters": {"type": "object", "properties": {k: {"type": TYPES[typ]} for k, (typ, _) in t.args.items()},
                       "required": [k for k, (_, req) in t.args.items() if req], "additionalProperties": False}}}
        for t in REGISTRY.values() if names is None or t.name in names]


AGENT_TOOLS = {  # identities from docs/use-cases.md; enforced by the gateway, not by call()
    "onboarding-agent": ["read_application", "read_documents", "extract_fields", "check_registry", "screen_sanctions",
                         "compute_risk", "create_client", "request_more_docs", "escalate_edd", "reject_application",
                         "send_email", "fetch_url", "run_code", "load_risk_model", "read_config"],
    "admin-agent": ["delete_client"],
}

import kyc, bait  # noqa: E402,F401  registers the tools
