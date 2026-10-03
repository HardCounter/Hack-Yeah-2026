"""Task Contract as read by the consume plane (docs/consumer-plane.md section 5.3)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Budget:
    tokens: int | None = None
    tool_calls: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class TaskContract:
    contract_id: str
    session_id: str
    agent_id: str
    role: str
    objective: str
    target_ids: frozenset[str]
    allowed_tools: frozenset[str]
    postconditions: tuple[str, ...]
    budget: Budget
    policy_version: str

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> TaskContract:
        budget = d.get("budget") or {}
        return cls(
            contract_id=d["contract_id"],
            session_id=d["session_id"],
            agent_id=d["agent_id"],
            role=d.get("role", ""),
            objective=d.get("objective", ""),
            target_ids=frozenset(d.get("target_ids", ())),
            allowed_tools=frozenset(d.get("allowed_tools", ())),
            postconditions=tuple(d.get("postconditions", ())),
            budget=Budget(budget.get("tokens"), budget.get("tool_calls"), budget.get("cost_usd")),
            policy_version=d.get("policy_version", "unknown"),
        )
