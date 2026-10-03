"""Normalized proposal; identity and metadata are bound by the trusted gateway."""
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, kw_only=True)
class ActionProposal:
    action_id: str
    session_id: str
    agent_id: str
    tool: str
    arguments: Mapping[str, Any]
    kind: str = "tool_use"
    side_effect: str = "read"
    transport: str = "inproc"
