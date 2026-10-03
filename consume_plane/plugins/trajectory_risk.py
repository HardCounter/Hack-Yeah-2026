"""Trajectory risk: expected loss = sum over executed steps of P(failure) x consequence.

Deterministic and explainable; see docs/trajectory-risk-model.md. The probabilities are heuristic
likelihood scores built from observable events, not calibrated probabilities.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from consume_plane.sdk import AdjustmentProposal, AgentAction, FindingDraft, Subscription, TaskContract

LEVELS = ("low", "medium", "high", "critical")

DEFAULTS: dict[str, Any] = {
    "base_probability": 0.02,
    # consequence of one executed step, by side-effect class; tool_consequence overrides per tool
    "consequence": {"read": 1.0, "write": 5.0, "irreversible": 10.0, "egress": 4.0},
    "tool_consequence": {
        "create_client": 8.0, "reject_application": 4.0, "escalate_edd": 2.0, "request_more_docs": 2.0,
        "delete_client": 10.0, "send_email": 6.0, "run_code": 9.0, "load_risk_model": 7.0,
    },
    # likelihood weight of each signal, combined with noisy-OR
    "signal_weights": {
        "gateway_blocked": 0.15,
        "gateway_redacted": 0.05,
        "gateway_alert": 0.10,
        "tool_error": 0.05,
        "out_of_contract_tool": 0.30,
        "out_of_scope_target": 0.25,
        "repeated_side_effect": 0.40,
        "repeated_read": 0.10,
        "missing_prerequisite": 0.50,
        "untrusted_external_content": 0.10,
        "budget_pressure": 0.10,
    },
    "prerequisites": {"create_client": ["screen_sanctions"]},
    "external_content_tools": ["fetch_url"],
    "repeated_read_threshold": 3,
    "budget_pressure_ratio": 0.8,
    "id_pattern": r"^[A-Z]{3}-\d{4}$",
    # expected-loss thresholds for each level
    "levels": {"medium": 3.0, "high": 8.0, "critical": 15.0},
    "approval_tools": ["*"],
}


# numeric tables are merged key by key; every other key (e.g. prerequisites) is replaced as a whole
MERGED_KEYS = frozenset({"consequence", "tool_consequence", "signal_weights", "levels"})


def _merge(base: dict, override: Mapping) -> dict:
    out = deepcopy(base)
    for k, v in override.items():
        out[k] = {**out[k], **v} if k in MERGED_KEYS and isinstance(v, Mapping) else deepcopy(v)
    return out


@dataclass(frozen=True)
class Signal:
    name: str
    weight: float
    event_id: str


@dataclass(frozen=True)
class StepRisk:
    event_id: str
    tool: str
    probability: float
    consequence: float

    @property
    def risk(self) -> float:
        return self.probability * self.consequence


@dataclass
class Assessment:
    steps: list[StepRisk] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    probability: float = 0.0
    level: str = "low"

    @property
    def expected_loss(self) -> float:
        return sum(s.risk for s in self.steps)


class RiskModel:
    def __init__(self, config: Mapping[str, Any] | None = None):
        self.cfg = _merge(DEFAULTS, config or {})
        unknown = set(self.cfg["signal_weights"]) - set(DEFAULTS["signal_weights"])
        if unknown:
            raise ValueError(f"unknown signal_weights: {sorted(unknown)}")
        self.id_re = re.compile(self.cfg["id_pattern"])

    def probability(self, signals: Sequence[Signal]) -> float:
        survive = 1.0 - self.cfg["base_probability"]
        for s in signals:
            survive *= 1.0 - s.weight
        return 1.0 - survive

    def level(self, expected_loss: float) -> str:
        level = "low"
        for name in LEVELS[1:]:
            if expected_loss >= self.cfg["levels"][name]:
                level = name
        return level

    def _consequence(self, a: AgentAction) -> tuple[str, float]:
        if a.kind == "egress":
            return f"egress:{a.payload.host}", self.cfg["consequence"]["egress"]
        tool = a.payload.tool
        return tool, self.cfg["tool_consequence"].get(tool, self.cfg["consequence"][a.payload.side_effect])

    def assess(self, actions: Sequence[AgentAction], contract: TaskContract | None) -> Assessment:
        cfg, out = self.cfg, Assessment()
        weights = cfg["signal_weights"]
        calls: Counter = Counter()
        executed_tools: set[str] = set()
        attempts, tokens = 0, 0
        budget_flagged = False

        def sig(name: str, a: AgentAction) -> Signal:
            return Signal(name, weights[name], a.event_id)

        for a in actions:
            if a.usage is not None:
                tokens += a.usage.input_tokens + a.usage.output_tokens
            if a.kind not in ("tool_use", "egress"):
                continue
            attempts += 1
            own: list[Signal] = []   # raise the probability of this step and every later one
            later: list[Signal] = [] # raise the probability of later steps only

            if a.status == "blocked":
                own.append(sig("gateway_blocked", a))
            elif a.status == "redacted":
                own.append(sig("gateway_redacted", a))
            elif a.status == "failed":
                own.append(sig("tool_error", a))
            if a.gateway is not None and a.gateway.final == "ALERT":
                own.append(sig("gateway_alert", a))

            if a.kind == "tool_use":
                tool = a.payload.tool
                if contract is not None and contract.allowed_tools and tool not in contract.allowed_tools:
                    own.append(sig("out_of_contract_tool", a))
                if contract is not None and contract.target_ids:
                    if any(isinstance(v, str) and self.id_re.match(v) and v not in contract.target_ids
                           for v in a.payload.args.values()):
                        own.append(sig("out_of_scope_target", a))
                if a.executed:
                    key = (tool, json.dumps(a.payload.args, sort_keys=True, default=str))
                    calls[key] += 1
                    if calls[key] > 1 and a.payload.side_effect != "read":
                        own.append(sig("repeated_side_effect", a))
                    elif calls[key] >= cfg["repeated_read_threshold"]:
                        own.append(sig("repeated_read", a))
                    missing = [p for p in cfg["prerequisites"].get(tool, ()) if p not in executed_tools]
                    if missing:
                        own.append(sig("missing_prerequisite", a))
                    executed_tools.add(tool)
                    if tool in cfg["external_content_tools"]:
                        later.append(sig("untrusted_external_content", a))
            elif a.executed:
                later.append(sig("untrusted_external_content", a))

            if contract is not None and not budget_flagged:
                b, ratio = contract.budget, cfg["budget_pressure_ratio"]
                if (b.tool_calls and attempts >= ratio * b.tool_calls) or (b.tokens and tokens >= ratio * b.tokens):
                    own.append(sig("budget_pressure", a))
                    budget_flagged = True

            out.signals.extend(own)
            if a.executed:
                tool_name, consequence = self._consequence(a)
                out.steps.append(StepRisk(a.event_id, tool_name, self.probability(out.signals), consequence))
            out.signals.extend(later)

        out.probability = self.probability(out.signals)
        out.level = self.level(out.expected_loss)
        return out


class TrajectoryRisk:
    name = "trajectory-risk"
    version = "1.0.0"
    method = "deterministic"
    subscription = Subscription(kinds=frozenset({"tool_use", "egress"}))

    async def setup(self, ctx):
        self.model = RiskModel(ctx.config)

    async def handle(self, action, ctx):
        trajectory = await ctx.trajectory()
        contract = await ctx.contract()
        now = self.model.assess(trajectory, contract)
        before = self.model.assess(trajectory[:-1], contract)

        labels = {"session_id": action.session_id, "agent": action.agent_id}
        ctx.emit_metric("trajectory_expected_loss", round(now.expected_loss, 4), **labels)
        ctx.emit_metric("trajectory_failure_probability", round(now.probability, 4), **labels)

        if LEVELS.index(now.level) <= LEVELS.index(before.level) or now.level == "low":
            return  # report only when the session enters a higher level

        top = sorted(now.steps, key=lambda s: s.risk, reverse=True)[:3]
        counts = Counter(s.name for s in now.signals)
        evidence = dict.fromkeys([s.event_id for s in now.signals] + [s.event_id for s in top])
        ctx.emit_finding(FindingDraft(
            rule_id=f"risk.trajectory_{now.level}",
            severity=now.level,
            summary=(f"trajectory risk {now.level}: expected loss {now.expected_loss:.2f} "
                     f"(P(failure) now {now.probability:.2f}); signals: "
                     + (", ".join(f"{n}x{c}" for n, c in counts.most_common()) or "none")),
            evidence_event_ids=tuple(evidence),
            details={
                "model": "sum(P(failure) x consequence), noisy-OR likelihood, heuristic weights",
                "expected_loss": round(now.expected_loss, 4),
                "failure_probability": round(now.probability, 4),
                "previous_level": before.level,
                "signals": dict(counts),
                "top_steps": [{"event_id": s.event_id, "tool": s.tool, "probability": round(s.probability, 4),
                               "consequence": s.consequence, "risk": round(s.risk, 4)} for s in top],
                "contract_found": contract is not None,
            },
        ))
        if now.level == "high":
            ctx.propose_adjustment(AdjustmentProposal(
                action="REQUIRE_APPROVAL_FOR", tools=tuple(self.model.cfg["approval_tools"]), ttl_s=900,
                reason=f"trajectory expected loss {now.expected_loss:.2f} reached 'high'"))
        elif now.level == "critical":
            ctx.propose_adjustment(AdjustmentProposal(
                action="HALT_SESSION", ttl_s=1800,
                reason=f"trajectory expected loss {now.expected_loss:.2f} reached 'critical'"))
