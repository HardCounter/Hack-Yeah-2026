import asyncio
from datetime import timedelta

import pytest

from conftest import T0, action
from consume_plane.adapters.memory import MemoryFeedbackChannel
from consume_plane.model.outputs import AdjustmentProposal
from consume_plane.runtime.config import FeedbackConfig
from consume_plane.runtime.feedback import FeedbackController

ALL = ["ALERT", "REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS", "STRICT_MODE", "HALT_SESSION"]


def controller(**cfg):
    cfg.setdefault("allowed_actions", {"p": ALL})
    channel = MemoryFeedbackChannel()
    return FeedbackController(FeedbackConfig(**cfg), channel), channel


def submit(ctrl, proposal, *, method="deterministic", seq=1, plugin="p", **action_kw):
    return asyncio.run(ctrl.submit(proposal, plugin=plugin, method=method, action=action(seq, **action_kw)))


def test_accepted_proposal_becomes_signal_in_layer1_format():
    ctrl, channel = controller(max_ttl_s=100)
    d = submit(ctrl, AdjustmentProposal(action="BLOCK_TOOLS", tools=("run_code",), ttl_s=600, reason="loop"))
    assert d.accepted
    [sig] = channel.signals
    assert sig.target_scope == {"session_id": "sess_1", "agent_id": "onboarding-agent"}
    assert sig.policy_modifications == {"blocked_tools": ["run_code"]}
    assert sig.ttl_seconds == 100 and sig.source_plugin == "p" and sig.trigger_event_id == "evt_sess_1_1"
    assert sig.ts == T0 + timedelta(seconds=1)


@pytest.mark.parametrize("cfg, proposal, method, reason", [
    ({"enabled": False}, AdjustmentProposal(action="ALERT"), "deterministic", "feedback_disabled"),
    ({}, AdjustmentProposal(action="HALT_SESSION"), "semantic", "semantic_not_allowed"),
    ({"allowed_actions": {"p": ["ALERT"]}}, AdjustmentProposal(action="BLOCK_TOOLS", tools=("x",)),
     "deterministic", "not_allowed_for_plugin"),
    ({"allowed_actions": {}}, AdjustmentProposal(action="ALERT"), "deterministic", "not_allowed_for_plugin"),
    ({}, AdjustmentProposal(action="ALERT", scope="agent"), "deterministic", "agent_scope_disabled"),
    ({}, AdjustmentProposal(action="BLOCK_TOOLS"), "deterministic", "missing_tools"),
    ({}, AdjustmentProposal(action="ALERT", ttl_s=0), "deterministic", "bad_ttl"),
])
def test_rejections(cfg, proposal, method, reason):
    ctrl, channel = controller(**cfg)
    d = submit(ctrl, proposal, method=method)
    assert not d.accepted and d.reason == reason
    assert channel.signals == [] and ctrl.log == [d]


def test_semantic_plugin_may_require_approval():
    ctrl, _ = controller()
    assert submit(ctrl, AdjustmentProposal(action="REQUIRE_APPROVAL_FOR", tools=("create_client",)),
                  method="semantic").accepted


def test_duplicates_are_merged_until_ttl_expires():
    ctrl, channel = controller()
    p = AdjustmentProposal(action="BLOCK_TOOLS", tools=("a", "b"), ttl_s=10)
    assert submit(ctrl, p, seq=1).accepted
    assert submit(ctrl, AdjustmentProposal(action="BLOCK_TOOLS", tools=("a",), ttl_s=10), seq=2).reason == "already_active"
    assert submit(ctrl, AdjustmentProposal(action="BLOCK_TOOLS", tools=("c",), ttl_s=10), seq=3).accepted
    assert submit(ctrl, p, seq=30).accepted                      # 29 s later the first one expired
    assert submit(ctrl, p, seq=31, session_id="sess_2").accepted  # other session is independent
    assert len(channel.signals) == 4


def test_halt_subsumes_everything():
    ctrl, _ = controller()
    assert submit(ctrl, AdjustmentProposal(action="HALT_SESSION"), seq=1).accepted
    assert submit(ctrl, AdjustmentProposal(action="ALERT"), seq=2).reason == "already_active"


def test_rate_limit_per_session():
    ctrl, _ = controller(max_signals_per_session_per_minute=2)
    for i, tool in enumerate(["a", "b"], 1):
        assert submit(ctrl, AdjustmentProposal(action="BLOCK_TOOLS", tools=(tool,)), seq=i).accepted
    assert submit(ctrl, AdjustmentProposal(action="BLOCK_TOOLS", tools=("c",)), seq=3).reason == "rate_limited"
    assert submit(ctrl, AdjustmentProposal(action="BLOCK_TOOLS", tools=("c",)), seq=70).accepted


def test_agent_scope_when_enabled():
    ctrl, channel = controller(allow_agent_scope=True)
    assert submit(ctrl, AdjustmentProposal(action="STRICT_MODE", scope="agent")).accepted
    assert channel.signals[0].target_scope == {"agent_id": "onboarding-agent"}
