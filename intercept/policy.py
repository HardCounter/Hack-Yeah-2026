"""Trusted configuration and atomic per-run tool-call admission."""
import hashlib
import json
import re
import threading

IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class Policy:
    def __init__(self, config):
        if set(config) != {"runs"} or not isinstance(config["runs"], dict):
            raise ValueError("policy requires a runs object")
        self.config = json.loads(json.dumps(config))
        for session, run in self.config["runs"].items():
            if not isinstance(run, dict):
                raise ValueError("invalid run contract")
            if not IDENTIFIER.fullmatch(session):
                raise ValueError("invalid session identifier")
            if set(run) != {"contract_id", "allowed_tools", "tool_call_budget", "require_approval", "argument_equals"}:
                raise ValueError("invalid run contract")
            if not isinstance(run["contract_id"], str) or not IDENTIFIER.fullmatch(run["contract_id"]):
                raise ValueError("invalid contract identifier")
            for field in ("allowed_tools", "require_approval"):
                if not isinstance(run[field], list) or any(not isinstance(t, str) or not IDENTIFIER.fullmatch(t) for t in run[field]):
                    raise ValueError("invalid tool list")
            if type(run["tool_call_budget"]) is not int or run["tool_call_budget"] < 0:
                raise ValueError("invalid tool budget")
            if not isinstance(run["argument_equals"], dict):
                raise ValueError("invalid argument constraints")
            if any(not isinstance(v, dict) for v in run["argument_equals"].values()):
                raise ValueError("argument constraints must be objects")
        self.version = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        self.lock = threading.Lock()
        self.used = {}
        self.seen = set()
        self.admitted = {}
        self.completed = set()

    def evaluate(self, action):
        if not isinstance(action, dict) or set(action) != {"session_id", "call_id", "tool", "arguments"}:
            raise ValueError("invalid action")
        for field in ("session_id", "call_id", "tool"):
            if not isinstance(action[field], str) or not IDENTIFIER.fullmatch(action[field]):
                raise ValueError("invalid action identifier")
        if not isinstance(action["arguments"], dict):
            raise ValueError("arguments must be an object")
        session, call, tool = (action[k] for k in ("session_id", "call_id", "tool"))
        with self.lock:
            run = self.config["runs"].get(session)
            code = "ALLOW"
            key = (session, call)
            if run is None:
                code = "UNKNOWN_RUN"
            elif key in self.seen:
                code = "REPLAY"
            elif len(self.seen) >= 4096:
                code = "ADMISSION_CAPACITY"
            elif tool not in run["allowed_tools"]:
                code = "TOOL_DENIED"
            elif any(k not in action["arguments"] or
                     json.dumps(action["arguments"][k], sort_keys=True) != json.dumps(v, sort_keys=True)
                     for k, v in run["argument_equals"].get(tool, {}).items()):
                code = "TASK_SCOPE"
            elif tool in run["require_approval"]:
                code = "APPROVAL_NOT_IMPLEMENTED"
            elif self.used.get(session, 0) >= run["tool_call_budget"]:
                code = "BUDGET_EXHAUSTED"
            if run is not None and len(self.seen) < 4096:
                self.seen.add(key)
            if code == "ALLOW":
                self.used[session] = self.used.get(session, 0) + 1
                self.admitted[key] = tool
            return {
                "decision": "ALLOW" if code == "ALLOW" else "BLOCK",
                "code": code,
                "policy_version": self.version,
                "contract_id": run["contract_id"] if run else None,
                "session_id": session, "call_id": call, "tool": tool,
                "tool_calls_used": self.used.get(session, 0),
            }

    def observe(self, observation):
        if not isinstance(observation, dict) or set(observation) != {"session_id", "call_id", "tool", "status"}:
            raise ValueError("invalid observation")
        for field in ("session_id", "call_id", "tool"):
            if not isinstance(observation[field], str) or not IDENTIFIER.fullmatch(observation[field]):
                raise ValueError("invalid observation identifier")
        if observation["status"] not in ("completed", "error"):
            raise ValueError("invalid observation status")
        key = (observation["session_id"], observation["call_id"])
        with self.lock:
            if key in self.completed or self.admitted.get(key) != observation["tool"]:
                raise ValueError("observation not matched to an open admission")
            self.completed.add(key)
            return {**observation, "policy_version": self.version,
                    "verification": "NOT_VERIFIED"}
