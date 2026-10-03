"""Builders for synthetic wire events (envelope v2.1) and a manager harness."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from consume_plane.adapters.memory import MemoryEventSource, MemoryFeedbackChannel, MemorySink, MemoryTrajectoryReader
from consume_plane.model.decode import decode_event
from consume_plane.runtime.config import ConsumePlaneConfig, FeedbackConfig, PluginEntry
from consume_plane.runtime.feedback import FeedbackController
from consume_plane.runtime.ledger import Ledger
from consume_plane.runtime.manager import ConsumerManager
from consume_plane.runtime.registry import load_registry

T0 = datetime(2026, 10, 3, 15, 42, 0, tzinfo=timezone.utc)


def wire_event(seq: int, *, session_id: str = "sess_1", action_type: str = "tool_call", tool: str = "read_application",
               args: dict | None = None, status: str = "completed", ts: datetime | None = None, **overrides) -> dict:
    details = {
        "tool_call": {"name": tool, "side_effect": "read", "transport": "inproc",
                      "parameters": args if args is not None else {"app_id": "APP-0001"},
                      "result": {"ref": f"store://agent_content/{session_id}-{seq}", "sha256": "0" * 64,
                                 "size_bytes": 12, "redacted": False, "trust": "untrusted"}},
        "llm_call": {"model": "llama3.1:8b", "provider": "ollama", "messages": [], "completion": None,
                     "tool_calls_requested": [], "stop_reason": "end_turn"},
        "session": {"phase": "started", "contract_id": f"ctr_{session_id}", "policy_version": "v1"},
    }.get(action_type, {"anything": True})
    event = {
        "schema_version": "2.1",
        "event_id": f"evt_{session_id}_{seq}",
        "seq": seq,
        "ts": (ts or T0 + timedelta(seconds=seq)).isoformat().replace("+00:00", "Z"),
        "run_id": "run_test",
        "session_id": session_id,
        "case_id": "APP-0001",
        "agent_id": "onboarding-agent",
        "step_id": seq,
        "parent_span_id": None,
        "action_type": action_type,
        "status": status,
        "action_details": details,
        "interception_metadata": {"final_decision": "ALLOW", "policy_version": "v1",
                                  "auditor_decisions": [{"auditor": "tool-allowlist", "decision": "ALLOW",
                                                         "latency_ms": 0.3}],
                                  "interception_overhead_ms": 0.9},
        "metrics": {"input_tokens": 0, "output_tokens": 0, "latency_ms": 5.0, "cost_usd": 0.0},
        "fault_injected": False,
    }
    event.update(overrides)
    return event


def action(seq: int, **kw):
    return decode_event(wire_event(seq, **kw))


class Harness:
    """Manager wired to in-memory adapters; the reader already holds every event (Layer 2 invariant)."""

    def __init__(self, actions, plugins, *, feedback: FeedbackConfig | None = None, plugin_config=None, **manager_kw):
        self.source = MemoryEventSource(actions)
        self.reader = MemoryTrajectoryReader(actions)
        self.sink = MemorySink()
        self.channel = MemoryFeedbackChannel()
        self.ledger = Ledger()
        self.plugins = plugins
        self.feedback_cfg = feedback or FeedbackConfig()
        self.plugin_config = plugin_config or {}
        self.manager_kw = {"poll_timeout_s": 0.01, "retry_backoff_s": 0.0, **manager_kw}
        self.manager: ConsumerManager | None = None

    async def run(self) -> ConsumerManager:
        cfg = ConsumePlaneConfig(plugins={name: PluginEntry(name=name, config=c)
                                          for name, c in self.plugin_config.items()})
        registry = await load_registry(cfg, self.plugins)
        self.manager = ConsumerManager(source=self.source, reader=self.reader, registry=registry, ledger=self.ledger,
                                       sinks=[self.sink], feedback=FeedbackController(self.feedback_cfg, self.channel),
                                       **self.manager_kw)
        await asyncio.wait_for(self.manager.run(stop_when_idle=True), timeout=10)
        return self.manager

    def findings(self, rule_id: str | None = None):
        return [f for f in self.sink.findings.values() if rule_id is None or f.rule_id == rule_id]


@pytest.fixture
def harness():
    return Harness
