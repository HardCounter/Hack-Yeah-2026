"""Layer 1 call-velocity gate: hold the over-limit proposal before dispatch."""
from collections import deque
from collections.abc import Mapping
import math
import threading
import time

from intercept.plugins import AuditDecision


class VelocityGuard:
    name = "velocity-guard"
    version = "2.0.0"
    method = "deterministic"

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.sessions = {}

    def setup(self, config):
        if not isinstance(config, Mapping) or set(config) - {"enabled", "window_s", "max_calls"}:
            raise ValueError("invalid velocity-guard configuration")
        self.window = config.get("window_s", 10)
        self.limit = config.get("max_calls", 8)
        if (type(config.get("enabled", True)) is not bool
                or type(self.window) not in (int, float) or not math.isfinite(self.window)
                or not 0 < self.window <= 3600
                or type(self.limit) is not int or not 1 <= self.limit <= 10000):
            raise ValueError("invalid velocity limits")

    def evaluate(self, ctx):
        if ctx.phase != "input" or ctx.action_type not in ("tool_call", "mcp_tool"):
            return AuditDecision("ALLOW")
        with self.lock:
            now = self.clock()
            cutoff = now - self.window
            # State is bounded even if this pipeline is shared across many sessions.
            for session in list(self.sessions):
                queue, seen = self.sessions[session]
                while queue and queue[0][0] < cutoff:
                    _, action_id = queue.popleft()
                    seen.pop(action_id, None)
                if not queue:
                    del self.sessions[session]
            if ctx.session_id not in self.sessions:
                if len(self.sessions) >= 128:
                    return AuditDecision("BLOCK", violation_code="VELOCITY_STATE_LIMIT")
                self.sessions[ctx.session_id] = (deque(), {})
            queue, seen = self.sessions[ctx.session_id]
            if ctx.action_id in seen:
                return seen[ctx.action_id]  # create-client is inspected twice; do not count twice
            if len(queue) >= self.limit:
                # Rejected attempts do not extend the hold forever or grow the cache.
                return AuditDecision("REQUIRE_APPROVAL", violation_code="VELOCITY_EXCEEDED",
                                     evidence={"calls_in_window": len(queue) + 1})
            result = AuditDecision("ALLOW")
            queue.append((now, ctx.action_id))
            seen[ctx.action_id] = result
            return result


PLUGINS = [VelocityGuard]
