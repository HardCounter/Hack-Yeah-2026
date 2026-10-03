"""Immutable trusted Task Contract shared by the three runtime layers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Budget:
    tokens: int | None = None
    tool_calls: int | None = None
    cost_usd: float | None = None

    def __post_init__(self):
        for value in (self.tokens, self.tool_calls):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("budget counts must be non-negative integers")
        if self.cost_usd is not None and (isinstance(self.cost_usd, bool) or not math.isfinite(self.cost_usd) or self.cost_usd < 0):
            raise ValueError("cost budget must be finite and non-negative")


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
    run_id: str | None = None
    principal_id: str | None = None
    case_id: str | None = None
    policy_hash: str | None = None
    feed_version: str | None = None

    def __post_init__(self):
        # Own immutable collections even if a caller passed lists/sets.
        object.__setattr__(self, "target_ids", frozenset(self.target_ids))
        object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))
        object.__setattr__(self, "postconditions", tuple(self.postconditions))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["target_ids"] = sorted(self.target_ids)
        d["allowed_tools"] = sorted(self.allowed_tools)
        d["postconditions"] = list(self.postconditions)
        return d

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
            run_id=d.get("run_id"),
            principal_id=d.get("principal_id"),
            case_id=d.get("case_id"),
            policy_hash=d.get("policy_hash"),
            feed_version=d.get("feed_version"),
        )
