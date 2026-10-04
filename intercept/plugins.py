"""Synchronous, trusted Layer 1 plugin contract and fixed registration list.

Raw payloads are transient. Plugins return reason codes, never matching content.
The consume-plane SDK is deliberately not used by these pre-dispatch auditors.
"""
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol


@dataclass(frozen=True)
class AuditContext:
    trace_id: str
    session_id: str
    agent_id: str
    action_type: Literal["llm_call", "tool_call", "mcp_tool", "egress_http"]
    action_name: str
    payload: Mapping[str, Any]
    current_policy_level: str
    task_contract: Mapping[str, Any]
    policy_version: str
    action_id: str
    phase: Literal["input", "output"] = "input"


@dataclass(frozen=True)
class AuditDecision:
    decision: Literal["ALLOW", "BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT"]
    reason: str | None = None
    violation_code: str | None = None
    modified_payload: dict | None = None
    evidence: Mapping[str, int] = field(default_factory=dict)


class ActionAuditorPlugin(Protocol):
    name: str
    version: str
    method: str

    def setup(self, config: Mapping[str, Any]) -> None: ...
    def evaluate(self, ctx: AuditContext) -> AuditDecision: ...


def registered_plugins():
    # Fixed trusted imports. Configuration/agent payloads cannot choose executable modules.
    from plugins import PLUGINS
    return {cls.name.replace("-", "_"): cls for cls in PLUGINS}


def load_plugins(config):
    registered = registered_plugins()
    if not isinstance(config, dict) or set(config) - set(registered):
        raise ValueError("unknown interception plugin")
    loaded = []
    for key, cls in registered.items():
        settings = config.get(key)
        if settings is None:
            continue
        plugin = cls()
        plugin.setup(settings)  # validate even disabled plugins before accepting a policy
        if settings.get("enabled", True):
            loaded.append(plugin)
    return loaded
