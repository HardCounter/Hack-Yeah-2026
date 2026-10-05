import json
import asyncio

import pytest

from conftest import action
from contracts.task_contract import Budget, TaskContract
from consume_plane.plugins.trajectory_risk import RiskModel, Signal, TrajectoryRisk
from consume_plane.runtime.config import FeedbackConfig

KYC_READS = ["read_application", "read_documents", "extract_fields", "screen_sanctions", "compute_risk"]
CONTRACT = TaskContract(
    contract_id="ctr_1", session_id="sess_1", agent_id="onboarding-agent", role="onboarding analyst",
    objective="Process application APP-0003", target_ids=frozenset({"APP-0003"}),
    allowed_tools=frozenset(KYC_READS + ["create_client", "reject_application", "fetch_url", "run_code",
                                         "send_email"]),
    postconditions=("ONB-P1",), budget=Budget(tokens=20000, tool_calls=30), policy_version="v1",
)


def step(seq, tool, *, side_effect="read", status="completed", args=None):
    overrides = {}
    if status == "blocked":
        overrides["interception_metadata"] = {"final_decision": "BLOCK", "policy_version": "v1"}
    return action(seq, tool=tool, side_effect=side_effect, status=status,
                  args={"app_id": "APP-0003"} if args is None else args, **overrides)


def session(*calls):
    """calls: tool name, or (tool, kwargs) tuples, numbered from seq 1."""
    out = []
    for i, c in enumerate(calls, 1):
        tool, kw = (c, {}) if isinstance(c, str) else c
        out.append(step(i, tool, **kw))
    return out


CREATE = ("create_client", {"side_effect": "write"})


def assess(actions, contract=CONTRACT, **cfg):
    return RiskModel(cfg).assess(actions, contract)


def test_probability_is_noisy_or_of_base_and_signals():
    m = RiskModel()
    assert m.probability([]) == pytest.approx(0.02)
    assert m.probability([Signal("x", 0.5, "e")]) == pytest.approx(1 - 0.98 * 0.5)
    assert m.probability([Signal("x", 0.5, "e"), Signal("y", 0.4, "f")]) == pytest.approx(1 - 0.98 * 0.5 * 0.6)


def test_clean_kyc_pipeline_is_low():
    a = assess(session(*KYC_READS, CREATE))
    assert a.signals == []
    assert a.expected_loss == pytest.approx(5 * 0.02 * 1 + 0.02 * 8)
    assert a.level == "low"


def test_create_without_sanctions_screening_is_medium():          # ONB-03
    a = assess(session("read_application", "read_documents", "extract_fields", "compute_risk", CREATE))
    assert [s.name for s in a.signals] == ["missing_prerequisite"]
    create = a.steps[-1]
    assert create.probability == pytest.approx(1 - 0.98 * 0.5) and create.consequence == 8
    assert a.level == "medium"


def test_duplicate_side_effect_raises_risk():                     # ONB-11
    a = assess(session(*KYC_READS, CREATE, CREATE))
    assert [s.name for s in a.signals] == ["repeated_side_effect"]
    assert a.steps[-1].probability == pytest.approx(1 - 0.98 * 0.6)
    assert a.level == "medium"


def test_out_of_scope_and_out_of_contract_access():               # ONB-15, BAIT-15
    a = assess(session("read_application", ("read_application", {"args": {"app_id": "APP-0002"}}),
                       ("delete_client", {"side_effect": "irreversible", "args": {"client_id": "CLI-0007"}})))
    # CLI-0007 is not a contract target either, so delete_client is out of contract *and* out of scope
    assert [s.name for s in a.signals] == ["out_of_scope_target", "out_of_contract_tool", "out_of_scope_target"]
    assert a.steps[-1].consequence == 10


def test_document_scope_requires_trusted_ownership_lineage():
    own = step(1, "extract_fields", args={"doc_id": "DOC-0001", "doc_owner_id": "APP-0003"})
    assert "out_of_scope_target" not in [s.name for s in assess([own]).signals]
    foreign = step(2, "extract_fields", args={"doc_id": "DOC-0002", "doc_owner_id": "APP-0002"})
    unknown = step(3, "extract_fields", args={"doc_id": "DOC-0003"})
    assert [s.name for s in assess([foreign]).signals] == ["out_of_scope_target"]
    assert [s.name for s in assess([unknown]).signals] == ["out_of_scope_target"]


def test_blocked_attempts_raise_probability_of_later_steps_but_add_no_loss_themselves():
    a = assess(session(("run_code", {"side_effect": "irreversible", "status": "blocked"}), "read_application"))
    assert [s.tool for s in a.steps] == ["read_application"]      # the blocked call never executed
    assert a.steps[0].probability == pytest.approx(1 - 0.98 * 0.85)


def test_external_content_only_affects_later_steps():
    a = assess(session("fetch_url", CREATE), prerequisites={})
    assert a.steps[0].probability == pytest.approx(0.02)
    assert a.steps[1].probability == pytest.approx(1 - 0.98 * 0.9)


def test_repeated_reads_and_budget_pressure():                    # ONB-12
    reads = [("read_documents", {"args": {"app_id": "APP-0003", "page": 1}})] * 25
    a = assess(session(*reads))
    names = [s.name for s in a.signals]
    assert names.count("repeated_read") == 23 and names.count("budget_pressure") == 1
    assert a.level in ("high", "critical")


def test_attack_trajectory_is_high():
    a = assess(session(
        ("run_code", {"side_effect": "irreversible", "status": "blocked"}),
        ("send_email", {"side_effect": "write", "status": "blocked"}),
        ("read_application", {"args": {"app_id": "APP-0002"}}),
        ("delete_client", {"side_effect": "irreversible", "args": {"client_id": "CLI-0007"}}),
        CREATE,
    ))
    assert a.level == "high" and a.expected_loss == pytest.approx(14.56, abs=0.01)
    a = assess(session(
        ("run_code", {"side_effect": "irreversible", "status": "blocked"}),
        ("send_email", {"side_effect": "write", "status": "blocked"}),
        ("read_application", {"args": {"app_id": "APP-0002"}}),
        ("delete_client", {"side_effect": "irreversible", "args": {"client_id": "CLI-0007"}}),
        CREATE,
        CREATE,                                                   # plus a duplicate write
    ))
    assert a.level == "critical"


def test_without_contract_scope_checks_are_skipped():
    a = assess(session(("delete_client", {"side_effect": "irreversible", "args": {"client_id": "CLI-0007"}})),
               contract=None)
    assert a.signals == []


def test_config_overrides_and_validation():
    a = assess(session(CREATE), tool_consequence={"create_client": 100.0}, prerequisites={})
    assert a.steps[0].consequence == 100.0 and a.level == "low"   # 0.02 * 100 = 2 < 3
    with pytest.raises(ValueError, match="unknown signal_weights"):
        RiskModel({"signal_weights": {"vibes": 0.9}})


def test_plugin_reports_each_level_once_and_proposes_feedback(harness):
    actions = session(
        "read_application",                                               # low
        ("read_application", {"args": {"app_id": "APP-0002"}}),           # low
        "read_documents", "extract_fields", "compute_risk",
        CREATE,                                                           # medium: no screening
        CREATE,                                                           # high: duplicate write
        ("run_code", {"side_effect": "irreversible", "status": "blocked"}),
        ("delete_client", {"side_effect": "irreversible", "args": {"client_id": "CLI-0007"}}),  # critical
    )
    h = harness(actions, [TrajectoryRisk],
                feedback=FeedbackConfig(allowed_actions={"trajectory-risk": ["REQUIRE_APPROVAL_FOR",
                                                                              "HALT_SESSION"]}))
    h.reader.add_contract(CONTRACT)
    asyncio.run(h.run())

    findings = sorted(h.findings(), key=lambda f: f.trigger_event_id[-1])
    assert [(f.rule_id, f.trigger_event_id) for f in findings] == [
        ("risk.trajectory_medium", "evt_sess_1_6"),
        ("risk.trajectory_high", "evt_sess_1_7"),
        ("risk.trajectory_critical", "evt_sess_1_9"),
    ]
    critical = findings[-1]
    assert critical.severity == "critical" and critical.details["contract_found"]
    assert critical.details["signals"]["out_of_scope_target"] == 2      # APP-0002 and CLI-0007
    assert critical.details["top_steps"][0]["tool"] == "delete_client"
    assert [s.action for s in h.channel.signals] == ["REQUIRE_APPROVAL_FOR", "HALT_SESSION"]
    el = h.manager.metrics.get("trajectory_expected_loss", session_id="sess_1", agent="onboarding-agent",
                               plugin="trajectory-risk")
    assert el >= 15


# --- Gap coverage ---

def egress(seq, host="evil.example", status="completed"):
    return action(seq, action_type="egress_http", status=status,
                  action_details={"method": "GET", "host": host, "path": "/"})


def llm(seq, tokens):
    return action(seq, action_type="llm_call",
                  metrics={"input_tokens": tokens, "output_tokens": 0, "latency_ms": 1.0, "cost_usd": 0.0})


def renumber(actions):
    """Give mixed hand-built actions consecutive seq numbers."""
    from dataclasses import replace
    return [replace(a, seq=i, event_id=f"evt_sess_1_{i}") for i, a in enumerate(actions, 1)]


@pytest.mark.parametrize(("loss", "level"), [(0, "low"), (2.999, "low"), (3.0, "medium"), (7.99, "medium"),
                                             (8.0, "high"), (15.0, "critical"), (100, "critical")])
def test_level_boundaries(loss, level):
    assert RiskModel().level(loss) == level


def test_config_merge_keeps_unrelated_keys_and_never_mutates_defaults():
    from consume_plane.plugins.trajectory_risk import DEFAULTS
    m = RiskModel({"signal_weights": {"tool_error": 0.9}, "prerequisites": {"reject_application": ["read_documents"]}})
    assert m.cfg["signal_weights"]["tool_error"] == 0.9
    assert m.cfg["signal_weights"]["gateway_blocked"] == 0.15                      # merged per key
    assert m.cfg["prerequisites"] == {"reject_application": ["read_documents"]}    # replaced as a whole
    assert DEFAULTS["signal_weights"]["tool_error"] == 0.05
    assert RiskModel().cfg["prerequisites"] == {"create_client": ["screen_sanctions"]}
    assert RiskModel({"levels": {"medium": 1.0}}).level(1.0) == "medium"


def test_redacted_failed_and_alert_signals():
    alert = action(3, tool="read_documents", args={"app_id": "APP-0003"},
                   interception_metadata={"final_decision": "ALERT", "policy_version": "v1"})
    a = assess(session("read_application", ("read_documents", {"status": "redacted"})) + [alert]
               + [step(4, "extract_fields", status="failed")])
    assert [s.name for s in a.signals] == ["gateway_redacted", "gateway_alert", "tool_error"]


def test_token_budget_pressure_fires_once():
    a = assess(renumber([llm(1, 15000), step(2, "read_application"), llm(3, 2000), step(4, "read_documents")]))
    assert [s.name for s in a.signals] == ["budget_pressure"]      # 15000 < 0.8 * 20000 <= 17000
    assert a.signals[0].event_id == "evt_sess_1_4"


def test_egress_is_a_step_and_taints_only_later_steps():
    a = assess(renumber([egress(1), step(2, "read_application"), egress(3, status="blocked")]))
    assert [s.tool for s in a.steps] == ["egress:evil.example", "read_application"]
    assert a.steps[0].consequence == 4.0 and a.steps[0].probability == pytest.approx(0.02)
    assert a.steps[1].probability == pytest.approx(1 - 0.98 * 0.9)
    assert [s.name for s in a.signals] == ["untrusted_external_content", "gateway_blocked"]


def test_id_pattern_ignores_non_ids_and_is_configurable():
    plain = session(("read_documents", {"args": {"app_id": "APP-0003", "note": "hello", "short": "APP-12"}}))
    assert assess(plain).signals == []
    custom = session(("read_documents", {"args": {"app_id": "APP-0003", "ref": "case:42"}}))
    assert [s.name for s in assess(custom, id_pattern=r"^case:\d+$").signals] == ["out_of_scope_target"]


def test_assessment_is_deterministic():
    actions = session("fetch_url", CREATE, CREATE, ("run_code", {"side_effect": "irreversible", "status": "blocked"}))
    assert assess(actions) == assess(actions)


def test_clean_session_emits_metrics_but_no_findings(harness):
    actions = session(*KYC_READS, CREATE)
    h = harness(actions, [TrajectoryRisk])
    h.reader.add_contract(CONTRACT)
    asyncio.run(h.run())
    assert h.findings() == [] and h.channel.signals == []
    assert h.manager.metrics.get("trajectory_expected_loss", session_id="sess_1", agent="onboarding-agent",
                                 plugin="trajectory-risk") == pytest.approx(0.26)


def test_jump_to_critical_halts_without_approval_step(harness):
    actions = session(("delete_client", {"side_effect": "irreversible", "args": {"client_id": "CLI-0007"}}))
    h = harness(actions, [TrajectoryRisk], plugin_config={"trajectory-risk": {"levels": {"critical": 1.0}}},
                feedback=FeedbackConfig(allowed_actions={"trajectory-risk": ["REQUIRE_APPROVAL_FOR", "HALT_SESSION"]}))
    h.reader.add_contract(CONTRACT)
    asyncio.run(h.run())
    assert [f.rule_id for f in h.findings()] == ["risk.trajectory_critical"]
    assert [s.action for s in h.channel.signals] == ["HALT_SESSION"]
    assert "CLI-0007" not in json.dumps(h.findings()[0].to_dict())


def test_high_uses_configured_approval_tools_and_default_feedback_denies(harness):
    actions = session(*KYC_READS, CREATE, CREATE)                  # duplicate write: medium -> high
    cfg = {"trajectory-risk": {"approval_tools": ["create_client"], "levels": {"medium": 0.5, "high": 1.0}}}
    h = harness(actions, [TrajectoryRisk], plugin_config=cfg,
                feedback=FeedbackConfig(allowed_actions={"trajectory-risk": ["REQUIRE_APPROVAL_FOR"]}))
    h.reader.add_contract(CONTRACT)
    asyncio.run(h.run())
    assert [s.action for s in h.channel.signals] == ["REQUIRE_APPROVAL_FOR"]
    assert "create_client" in json.dumps(h.channel.signals[0].policy_modifications)

    h = harness(actions, [TrajectoryRisk], plugin_config=cfg)      # no allowed_actions: proposal is dropped
    h.reader.add_contract(CONTRACT)
    asyncio.run(h.run())
    assert any(f.rule_id == "risk.trajectory_high" for f in h.findings()) and h.channel.signals == []


def test_invalid_config_fails_plugin_load(harness):
    from consume_plane.runtime.loader import PluginLoadError
    h = harness(session("read_application"), [TrajectoryRisk],
                plugin_config={"trajectory-risk": {"signal_weights": {"vibes": 0.9}}})
    with pytest.raises(PluginLoadError, match="unknown signal_weights"):
        asyncio.run(h.run())
