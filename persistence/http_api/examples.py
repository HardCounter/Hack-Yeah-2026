"""Static example payloads for the read API stub (docs/rest.md section 5).

Every builder takes the IDs from the request path so the dashboard can click through,
but the content is fixed sample data. Nothing here reads the evidence store.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SESSION = "sess_onb_APP0003"
RUN = "run_0040"
AGENT = "onboarding-agent"
CASE = "APP-0003"
CONTRACT = "contract_APP0003_v1"
POLICY = "v12"
FEED = "local-signatures-v1"
POLICY_HASH = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
MODEL = "claude-haiku-4-5-20251001"
T0 = "2026-10-03T15:40:00.000Z"
T_END = "2026-10-03T15:43:02.000Z"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ts(second: int) -> str:
    return f"2026-10-03T15:4{second // 60}:{second % 60:02d}.000Z"


def usage(input_tokens: int = 0, output_tokens: int = 0, cost: float = 0.0,
          latency: float = 0.0, source: str = "estimated") -> dict[str, Any]:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens, "cost_usd": cost,
            "latency_ms": latency, "source": source}


# (seq, kind, action_type, name, side_effect, status, decision, rule, reason_code, second)
_STEPS = [
    (0, "session", "session", "started", None, "completed", None, None, None, 0),
    (1, "prompt", "llm_call", MODEL, None, "completed", "ALLOW", None, None, 4),
    (2, "tool_use", "tool_call", "read_application", "read", "completed", "ALLOW", None, None, 9),
    (3, "tool_use", "tool_call", "read_application", "read", "completed", "ALERT", "SCOPE-01", "OUT_OF_SCOPE_TARGET", 15),
    (4, "prompt", "llm_call", MODEL, None, "completed", "ALLOW", None, None, 22),
    (5, "tool_use", "tool_call", "create_client", "write", "completed", "ALLOW", None, None, 40),
    (6, "tool_use", "tool_call", "create_client", "write", "pending_approval", "REQUIRE_APPROVAL",
     "TRAJ-APPROVAL", "FEEDBACK_REQUIRE_APPROVAL", 61),
    (7, "tool_use", "tool_call", "run_code", "irreversible", "blocked", "BLOCK", "SIG-003", "SIGNATURE_MATCH", 75),
    (8, "control", "control", "adjustment_applied", None, "completed", "ALLOW", None, None, 76),
    (9, "session", "session", "ended", None, "completed", None, None, None, 182),
]
_SEVERITY_FOR_DECISION = {"BLOCK": "high", "ALERT": "medium", "REQUIRE_APPROVAL": "medium", "REDACT": "low"}


def event_id(session_id: str, seq: int) -> str:
    return f"evt_{session_id}_{seq:04d}"


def _step_usage(kind: str, seq: int) -> dict[str, Any]:
    if kind == "prompt":
        return usage(812 + 40 * seq, 240, 0.0021, 640.0)
    if kind == "tool_use":
        return usage(latency=35.0 + seq)
    return usage()


def action_summary(session_id: str, seq: int) -> dict[str, Any]:
    s, kind, _, name, side_effect, status, decision, rule, reason, second = _STEPS[seq % len(_STEPS)]
    eid = event_id(session_id, s)
    detections = []
    if decision in _SEVERITY_FOR_DECISION:
        detections.append({"detection_id": f"gw_{eid}", "name": rule, "severity": _SEVERITY_FOR_DECISION[decision]})
    if s == 7:
        detections.append({"detection_id": "fnd_3b1c9a0e4f2d8c7b6a5e4d3c", "name": "risk.trajectory_critical",
                           "severity": "critical"})
    order = ["info", "low", "medium", "high", "critical"]
    return {
        "event_id": eid,
        "action_id": f"act_{s}" if kind not in ("session", "control") else None,
        "seq": s,
        "run_index": 2 * s,
        "ts": ts(second),
        "session_id": session_id,
        "run_id": RUN,
        "agent_id": AGENT,
        "case_id": CASE,
        "kind": kind,
        "name": name,
        "side_effect": side_effect,
        "status": status,
        "executed": status in ("completed", "redacted"),
        "decision": decision,
        "triggered_rules": ([{"auditor": "signature-scanner" if rule == "SIG-003" else "policy",
                              "decision": decision, "rule_id": rule}] if rule else []),
        "reason_code": reason,
        "policy_version": POLICY,
        "usage": _step_usage(kind, s),
        "interception_overhead_ms": 1.2 if decision else None,
        "detections": detections,
        "max_severity": max((d["severity"] for d in detections), key=order.index) if detections else None,
        "fault_injected": False,
    }


def _wire_details(kind: str, name: str, side_effect: str | None) -> dict[str, Any]:
    if kind == "prompt":
        return {"model": name, "provider": "anthropic", "messages": [], "completion": None,
                "tool_calls_requested": [], "stop_reason": "end_turn"}
    if kind == "tool_use":
        return {"name": name, "side_effect": side_effect, "transport": "inproc",
                "parameters": {"application_id": CASE}, "result": None, "error": None}
    if kind == "session":
        return {"phase": name, "contract_id": CONTRACT, "policy_version": POLICY,
                "end_reason": "agent_finished" if name == "ended" else None}
    return {"change": name, "policy_version": POLICY, "signal_id": "sig_9f2e01"}


def action(event_id_value: str) -> dict[str, Any]:
    session_id, seq = SESSION, 7
    if event_id_value.startswith("evt_") and event_id_value[-4:].isdigit():
        session_id, seq = event_id_value[4:-5] or SESSION, int(event_id_value[-4:]) % len(_STEPS)
    summary = action_summary(session_id, seq)
    summary["event_id"] = event_id_value
    _, kind, action_type, name, side_effect, status, decision, rule, _, _ = _STEPS[seq]
    event = {
        "schema_version": "2.1",
        "event_id": event_id_value,
        "action_id": summary["action_id"],
        "seq": seq,
        "ts": summary["ts"],
        "run_id": RUN,
        "trace_id": RUN,
        "session_id": session_id,
        "case_id": CASE,
        "agent_id": AGENT,
        "step_id": summary["run_index"],
        "parent_span_id": None,
        "action_type": action_type,
        "status": status,
        "action_details": _wire_details(kind, name, side_effect),
        "metrics": {k: summary["usage"][k] for k in ("input_tokens", "output_tokens", "latency_ms", "cost_usd")},
        "fault_injected": False,
    }
    if decision is not None:
        event["interception_metadata"] = {
            "final_decision": decision, "policy_version": POLICY,
            "auditor_decisions": [{"auditor": r["auditor"], "decision": r["decision"], "rule_id": r["rule_id"],
                                   "latency_ms": 0.8} for r in summary["triggered_rules"]],
            "interception_overhead_ms": 1.2,
        }
    related = []
    if kind == "tool_use" and side_effect != "read":
        related.append({"event_id": f"{event_id_value}_intent", "status": "pending",
                        "ts": summary["ts"], "relation": "intent"})
    return {
        "summary": summary,
        "event": event,
        "context": {
            "contract_id": CONTRACT, "principal_id": "op_1", "policy_hash": POLICY_HASH,
            "feed_version": FEED, "approval_id": None, "intervention_id": None,
            "effect_receipt_id": None, "semantic_model": None, "semantic_model_version": None,
            "reserved_usage": {"reserved_tokens": 2048} if kind == "prompt" else {},
        },
        "related": related,
        "previous_event_id": event_id(session_id, seq - 1) if seq > 0 else None,
        "next_event_id": event_id(session_id, seq + 1) if seq < len(_STEPS) - 1 else None,
        "detections": [detection(d["detection_id"]) for d in summary["detections"]],
    }


def steps(session_id: str) -> list[dict[str, Any]]:
    return [action_summary(session_id, s[0]) for s in _STEPS]


def detection(detection_id: str) -> dict[str, Any]:
    base = {
        "detection_id": detection_id, "detector_version": None, "method": "deterministic",
        "confidence": None, "session_id": SESSION, "run_id": RUN, "agent_id": AGENT, "case_id": CASE,
        "policy_version": POLICY, "details": {},
    }
    if detection_id.startswith("fnd_"):
        return {**base, "name": "risk.trajectory_critical", "ts": ts(76), "reason": "OUT_OF_CONTRACT_TOOL",
                "reason_text": "The agent called a tool that is not in its Task Contract.",
                "severity": "critical", "source": "finding", "detector": "trajectory-risk",
                "detector_version": "1.0", "action_taken": "HALT_SESSION",
                "trigger_event_id": event_id(SESSION, 7),
                "evidence_event_ids": [event_id(SESSION, n) for n in (3, 5, 6, 7)],
                "owasp": ["LLM06:2025 Excessive Agency"],
                "details": {"expected_loss": 22.77, "failure_probability": 0.95,
                            "signals": {"out_of_contract_tool": 1, "out_of_scope_target": 2}}}
    if detection_id.startswith("ver_"):
        return {**base, "session_id": detection_id[4:] or SESSION, "name": "verification.incomplete",
                "ts": T_END, "reason": "SCREENING_EVIDENCE_MISSING",
                "reason_text": "Sanctions screening evidence for the applicant was not found.",
                "severity": "high", "source": "verification", "detector": "outcome-verifier",
                "action_taken": "NONE", "trigger_event_id": event_id(SESSION, 9),
                "evidence_event_ids": [event_id(SESSION, 9)], "owasp": []}
    if detection_id.startswith("alr_"):
        return {**base, "name": "velocity.burst", "ts": ts(41), "reason": "TOOL_RATE_EXCEEDED",
                "reason_text": "Tool calls arrived faster than the configured rate.",
                "severity": "medium", "source": "alert", "detector": "velocity-guard",
                "action_taken": "ALERT", "trigger_event_id": event_id(SESSION, 5),
                "evidence_event_ids": [event_id(SESSION, 5)], "owasp": ["LLM10:2025 Unbounded Consumption"]}
    seq = 7
    if detection_id.startswith("gw_evt_") and detection_id[-4:].isdigit():
        seq = int(detection_id[-4:]) % len(_STEPS)
    _, _, _, _, _, _, decision, rule, reason, second = _STEPS[seq]
    decision = decision if decision in _SEVERITY_FOR_DECISION else "BLOCK"
    return {**base, "name": rule or "signature-scanner", "ts": ts(second), "reason": reason or "SIGNATURE_MATCH",
            "reason_text": "The request matched a known exploit signature.",
            "severity": _SEVERITY_FOR_DECISION[decision], "source": "gateway",
            "detector": "signature-scanner", "action_taken": decision,
            "trigger_event_id": event_id(SESSION, seq), "evidence_event_ids": [event_id(SESSION, seq)],
            "owasp": ["LLM01:2025 Prompt Injection"]}


def detections() -> list[dict[str, Any]]:
    ids = ["fnd_3b1c9a0e4f2d8c7b6a5e4d3c", f"ver_{SESSION}", f"gw_{event_id(SESSION, 7)}",
           f"gw_{event_id(SESSION, 6)}", "alr_7d1e2f", f"gw_{event_id(SESSION, 3)}"]
    return sorted((detection(i) for i in ids), key=lambda d: d["ts"], reverse=True)


def catalog() -> dict[str, Any]:
    return {"catalog_version": "c1", "entries": [
        {"name": "risk.trajectory_critical", "title": "Trajectory risk critical",
         "description": "Cumulative expected loss of the session crossed the critical threshold.",
         "default_severity": "critical",
         "reasons": {"OUT_OF_CONTRACT_TOOL": "The agent called a tool that is not in its Task Contract."},
         "owasp": ["LLM06:2025 Excessive Agency"], "control_family": "trajectory"},
        {"name": "SIG-003", "title": "Known exploit signature",
         "description": "The request matched an entry of the historical exploit signature feed.",
         "default_severity": "high",
         "reasons": {"SIGNATURE_MATCH": "The request matched a known exploit signature."},
         "owasp": ["LLM01:2025 Prompt Injection"], "control_family": "exploit_signature"},
        {"name": "verification.incomplete", "title": "Outcome not verified",
         "description": "Independent verification could not confirm every postcondition.",
         "default_severity": "high",
         "reasons": {"SCREENING_EVIDENCE_MISSING": "Sanctions screening evidence for the applicant was not found."},
         "owasp": [], "control_family": "outcome"},
    ]}


def contract(session_id: str) -> dict[str, Any]:
    return {"contract_id": CONTRACT, "role": "kyc_onboarding", "target_ids": [CASE],
            "allowed_tools": ["read_application", "screen_sanctions", "create_client"],
            "postconditions": ["ONB-P1", "ONB-P2", "ONB-P6"],
            "budget": {"tokens": 20000, "tool_calls": 30, "cost_usd": 0.5},
            "policy_version": POLICY, "policy_hash": POLICY_HASH, "feed_version": FEED}


def intervention(session_id: str = SESSION) -> dict[str, Any]:
    return {"signal_id": "sig_9f2e01", "ts": ts(76), "session_id": session_id, "action": "HALT_SESSION",
            "tools": [], "scope": "session", "ttl_seconds": 1800, "expires_at": "2026-10-03T16:11:16.000Z",
            "source_plugin": "trajectory-risk", "trigger_event_id": event_id(session_id, 7),
            "applied": True, "applied_event_id": event_id(session_id, 8), "active": True,
            "reason": "RISK_CRITICAL"}


def verification(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "verification_status": "VERIFICATION_INCOMPLETE",
            "verified_at": "2026-10-03T15:43:03.000Z", "checks": [
                {"id": "KYC-SCREENING-EVIDENCE", "status": "INCOMPLETE", "detail": "SCREENING_EVIDENCE_MISSING",
                 "evidence_source": None},
                {"id": "ONB-P2", "status": "PASS", "detail": None, "evidence_source": "bank_snapshot"}]}


def session_summary(session_id: str = SESSION, index: int = 0) -> dict[str, Any]:
    states = ["halted", "ended", "active"]
    return {"session_id": session_id, "run_id": RUN, "agent_id": AGENT, "case_id": CASE,
            "contract_id": CONTRACT, "policy_version": POLICY, "state": states[index % 3],
            "started_at": T0, "ended_at": T_END if index % 3 != 2 else None,
            "end_reason": "agent_finished" if index % 3 == 1 else None,
            "action_count": 10, "blocked_count": 1, "detection_count": 6, "max_severity": "critical",
            "risk_level": "critical", "verification_status": "VERIFICATION_INCOMPLETE",
            "usage": usage(1784, 480, 0.0042, 1280.0)}


def sessions() -> list[dict[str, Any]]:
    return [session_summary("sess_onb_APP0003", 0), session_summary("sess_onb_APP0001", 1),
            session_summary("sess_onb_APP0007", 2)]


def session_detail(session_id: str) -> dict[str, Any]:
    return {**session_summary(session_id), "contract": contract(session_id),
            "run": {"lifecycle": "SEALED", "sealed_at": T_END, "expired_at": None, "total_events": 13},
            "detections_by_severity": {"info": 0, "low": 0, "medium": 3, "high": 2, "critical": 1},
            "active_interventions": [intervention(session_id)], "verification": verification(session_id)}


def trajectory(scope: str, scope_id: str, view: str) -> dict[str, Any]:
    session_id = scope_id if scope == "session" else SESSION
    rows = [action(s["event_id"]) for s in steps(session_id)] if view == "full" else steps(session_id)
    segment = {"session_id": session_id, "run_id": RUN, "agent_id": AGENT, "case_id": CASE,
               "contract_id": CONTRACT, "state": "halted", "started_at": T0, "ended_at": T_END,
               "end_reason": "agent_finished", "steps": rows, "gaps": []}
    return {
        "scope": scope, "id": scope_id,
        "ordering": {"session": "seq", "run": "run_index"}.get(scope, "ts"),
        "segments": [segment],
        "totals": {"steps": 10, "executed": 8, "blocked": 1, "redacted": 0, "pending_approval": 1,
                   "failed": 0, "detections": 6, "max_severity": "critical",
                   "usage": usage(1784, 480, 0.0042, 1280.0)},
        "risk": {"level": "critical", "expected_loss": 22.77, "failure_probability": 0.95,
                 "finding_id": "fnd_3b1c9a0e4f2d8c7b6a5e4d3c", "source": "trajectory-risk"},
        "verification_status": "VERIFICATION_INCOMPLETE" if scope in ("session", "run") else None,
    }


def session_usage(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "window": {"first_ts": T0, "last_ts": T_END, "duration_s": 182.0},
        "usage": usage(1784, 480, 0.0042, 1280.0),
        "model_calls": {"total": 2, "completed": 2, "blocked": 0, "failed": 0, "by_model": {MODEL: 2}},
        "tool_calls": {"total": 5, "executed": 3, "blocked": 1, "pending_approval": 1, "failed": 0,
                       "by_tool": [{"tool": "read_application", "total": 2, "blocked": 0, "side_effect": "read"},
                                   {"tool": "create_client", "total": 2, "blocked": 0, "side_effect": "write"},
                                   {"tool": "run_code", "total": 1, "blocked": 1, "side_effect": "irreversible"}]},
        "egress_calls": {"total": 0, "blocked": 0, "by_host": {}},
        "latency_ms": {"backend_sum": 1280.0, "action_p50": 41.0, "action_p95": 640.0,
                       "interception_overhead_p50": 0.9, "interception_overhead_p95": 2.4},
        "budgets": [
            {"resource": "tokens", "limit": 20000, "used": 2264, "reserved": 0, "remaining": 17736,
             "utilisation": 0.113, "exceeded": False, "scope": "session"},
            {"resource": "tool_calls", "limit": 30, "used": 5, "reserved": 0, "remaining": 25,
             "utilisation": 0.167, "exceeded": False, "scope": "session"},
            {"resource": "cost_usd", "limit": 0.5, "used": 0.0042, "reserved": 0, "remaining": 0.4958,
             "utilisation": 0.008, "exceeded": False, "scope": "session"}],
        "budget_blocks": 0,
    }


def usage_report(group_by: str, since: str, until: str) -> dict[str, Any]:
    keys = {"agent": [AGENT, "aml-agent"], "session": ["sess_onb_APP0003", "sess_onb_APP0001"],
            "case": [CASE, "APP-0001"], "model": [MODEL, "claude-sonnet-5-5"],
            "tool": ["create_client", "read_application"], "day": ["2026-10-03", "2026-10-04"]}[group_by]
    buckets = [{"key": k, "sessions": 6 - 2 * i, "model_calls": 40 - 15 * i, "tool_calls": 70 - 20 * i,
                "blocked": 5 - 2 * i, "usage": usage(32000 - 9000 * i, 9000 - 2000 * i, 0.08 - 0.03 * i, 21000.0),
                "budget": ({"resource": "cost_usd", "limit": 5.0, "used": 0.08 - 0.03 * i, "reserved": 0,
                            "remaining": 4.92 + 0.03 * i, "utilisation": 0.016 - 0.006 * i, "exceeded": False,
                            "scope": "day"} if group_by == "day" else None)}
               for i, k in enumerate(keys)]
    return {"group_by": group_by, "since": since, "until": until,
            "totals": usage(55000, 16000, 0.13, 42000.0), "buckets": buckets}


def security_overview(since: str, until: str, top: int) -> dict[str, Any]:
    return {
        "since": since, "until": until,
        "actions": {"total": 412,
                    "by_decision": {"ALLOW": 360, "BLOCK": 31, "REDACT": 14, "REQUIRE_APPROVAL": 3, "ALERT": 4},
                    "by_kind": {"prompt": 120, "tool_use": 280, "egress": 0, "session": 8, "approval": 0, "control": 4}},
        "block_rate": 0.075, "redact_rate": 0.034,
        "detections": {"total": 58,
                       "by_severity": {"info": 0, "low": 14, "medium": 20, "high": 19, "critical": 5},
                       "by_source": {"gateway": 52, "finding": 4, "alert": 1, "verification": 1},
                       "by_control_family": {"secrets": 9, "injection": 12, "pii": 14, "budget": 2,
                                             "exploit_signature": 10, "trajectory": 4, "outcome": 1, "other": 6}},
        "top_detections": [{"name": "SIG-003", "count": 17, "max_severity": "high"},
                           {"name": "PII-PESEL", "count": 11, "max_severity": "low"},
                           {"name": "risk.trajectory_critical", "count": 1, "max_severity": "critical"}][:top],
        "top_blocked_tools": [{"tool": "run_code", "count": 6}, {"tool": "delete_client", "count": 3}][:top],
        "sessions": {"total": 8, "active": 1, "halted": 1,
                     "by_risk_level": {"low": 5, "medium": 1, "high": 1, "critical": 1}},
        "verification": {"VERIFIED_SUCCESS": 5, "FAILED_POSTCONDITIONS": 1, "VERIFICATION_INCOMPLETE": 1, "none": 1},
        "interventions": {"proposed": 3, "applied": 3, "active": 1},
        "interception_overhead_ms": {"p50": 0.9, "p95": 2.4, "p99": 6.1},
        "policy_versions": [POLICY], "feed_versions": [FEED],
    }


def performance_overview(since: str, until: str) -> dict[str, Any]:
    # 404 gateway-evaluated actions: the 412 of security_overview minus 8 session events.
    return {
        "since": since, "until": until,
        "actions_evaluated": 404,
        "interception_overhead_ms": {"p50": 1.1, "p95": 228.0, "p99": 252.0},
        "by_method": {
            "deterministic": {"runs": 404, "skipped": 0, "p50": 0.9, "p95": 2.4, "p99": 6.1},
            "semantic": {"runs": 148, "skipped": 24, "p50": 152.0, "p95": 234.0, "p99": 255.0}},
        "backend_latency_ms": {"p50": 41.0, "p95": 640.0, "p99": 910.0},
        "overhead_share": 0.22,
    }


def store_stats() -> dict[str, Any]:
    return {"total_events": 4120, "total_alerts": 3, "total_findings": 12, "total_dlq_records": 0,
            "events_by_type": {"TOOL_CALL": 2800, "LLM_INVOCATION": 1200, "SESSION": 100, "CONTROL": 20},
            "events_by_status": {"EXECUTED": 3700, "BLOCKED": 310, "PENDING": 110},
            "alerts_by_severity": {"HIGH": 2, "CRITICAL": 1}, "average_latency_ms": 1.3,
            "pending_deliveries": {"trajectory-risk": 0, "outcome-verifier": 0, "live-feed": 2},
            "db_size_bytes": 5242880, "schema_version": 3}
