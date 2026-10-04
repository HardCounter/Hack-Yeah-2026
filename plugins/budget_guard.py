"""Per-session in-memory budget reservations for Layer 1.

`budget` stays at the top level of the selected preset. The composition root passes that
same mapping to this plugin; this class is not independently configurable in JSON.
Existing Policy and PromptGateway checks remain hard backstops.
"""
from collections import OrderedDict
from collections.abc import Mapping
import threading

from intercept.plugins import AuditDecision

MAX_TRACKED_ACTIONS = 4096


class BudgetGuard:
    name = "budget-guard"
    version = "1.0.0"
    method = "deterministic"

    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}

    def setup(self, config):
        if not isinstance(config, Mapping) or set(config) != {"tokens", "tool_calls", "cost_usd"}:
            raise ValueError("BudgetGuard requires the preset's top-level budget object")
        self.token_limit = config["tokens"]
        self.tool_call_limit = config["tool_calls"]
        self.cost_limit = config["cost_usd"]
        if (type(self.token_limit) is not int or not 1 <= self.token_limit <= 1_000_000
                or type(self.tool_call_limit) is not int or not 1 <= self.tool_call_limit <= 1000
                or (self.cost_limit is not None and
                    (type(self.cost_limit) not in (int, float) or not 0 <= self.cost_limit <= 1000))):
            raise ValueError("invalid budget limits")
        self.sessions = {}

    def _state(self, session_id):
        if session_id not in self.sessions:
            if len(self.sessions) >= 128:
                return None
            self.sessions[session_id] = {
                "tokens_used": 0,
                "tool_calls_used": 0,
                "reservations": OrderedDict(),
            }
        return self.sessions[session_id]

    def evaluate(self, ctx):
        """Report the session's current counters without charging this proposal."""
        with self.lock:
            state = self._state(ctx.session_id)
            if state is None:
                return AuditDecision("BLOCK", violation_code="BUDGET_STATE_LIMIT")
            return AuditDecision("ALLOW", evidence={
                "tokens_used": state["tokens_used"],
                "tokens_limit": self.token_limit,
                "tool_calls_used": state["tool_calls_used"],
                "tool_calls_limit": self.tool_call_limit,
            })

    def reserve(self, ctx, *, tokens=0, tool_calls=0, known_tokens=0):
        """Atomically charge an admitted dispatch. Replays of the same action are idempotent."""
        if (type(tokens) is not int or tokens < 0 or type(tool_calls) is not int or tool_calls not in (0, 1)
                or type(known_tokens) is not int or known_tokens < 0):
            return AuditDecision("BLOCK", violation_code="INVALID_BUDGET_USAGE")
        with self.lock:
            state = self._state(ctx.session_id)
            if state is None:
                return AuditDecision("BLOCK", violation_code="BUDGET_STATE_LIMIT")
            state["tokens_used"] = max(state["tokens_used"], min(known_tokens, self.token_limit))
            reservations = state["reservations"]
            prior = reservations.get(ctx.action_id)
            charge = (tokens, tool_calls)
            if prior is not None:
                if prior != charge:
                    return AuditDecision("BLOCK", violation_code="BUDGET_RESERVATION_CONFLICT")
                reservations.move_to_end(ctx.action_id)
                return AuditDecision("ALLOW", evidence=self._evidence(state))
            if len(reservations) >= MAX_TRACKED_ACTIONS:
                return AuditDecision("BLOCK", violation_code="BUDGET_ACTION_CAPACITY")

            if ctx.action_type == "llm_call" and self.cost_limit not in (None, 0, 0.0):
                # Local inference has no trusted price/usage source. Never guess a dollar charge.
                return AuditDecision("BLOCK", violation_code="COST_BUDGET_UNSUPPORTED",
                                     evidence=self._evidence(state))
            if tokens and state["tokens_used"] + tokens > self.token_limit:
                return AuditDecision("BLOCK", violation_code="TOKEN_BUDGET_EXHAUSTED",
                                     evidence=self._evidence(state))
            if tool_calls and state["tool_calls_used"] + tool_calls > self.tool_call_limit:
                return AuditDecision("BLOCK", violation_code="TOOL_CALL_BUDGET_EXHAUSTED",
                                     evidence=self._evidence(state))

            state["tokens_used"] += tokens
            state["tool_calls_used"] += tool_calls
            reservations[ctx.action_id] = charge
            return AuditDecision("ALLOW", evidence=self._evidence(state))

    def report_limit(self, session_id, code):
        """Return evidence for the existing durable hard-budget denial path."""
        with self.lock:
            state = self._state(session_id)
            if state is None:
                return AuditDecision("BLOCK", violation_code="BUDGET_STATE_LIMIT")
            allowed = {"TOKEN_BUDGET_EXHAUSTED", "TOOL_CALL_BUDGET_EXHAUSTED", "COST_BUDGET_UNSUPPORTED",
                       "BUDGET_EXHAUSTED"}
            if code not in allowed:
                return AuditDecision("BLOCK", violation_code="INVALID_BUDGET_REASON")
            return AuditDecision("BLOCK", violation_code=code, evidence=self._evidence(state))

    def _evidence(self, state):
        return {
            "tokens_used": state["tokens_used"],
            "tokens_limit": self.token_limit,
            "tool_calls_used": state["tool_calls_used"],
            "tool_calls_limit": self.tool_call_limit,
        }


PLUGINS = [BudgetGuard]
