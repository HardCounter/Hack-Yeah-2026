"""Generated example data for the read API's explicit example mode (docs/rest.md section 5).

Nothing here reads the evidence store. The data is a rolling window of synthetic agent sessions
derived from the clock: a new session starts every ``PERIOD`` seconds and its steps appear as their
timestamps pass, so a polling dashboard sees a live feed. Every aggregate is computed from the same
sessions, so the endpoints agree with each other. An unknown session ID replays the worked example
of docs/trajectory-risk-model.md under that ID, so any path the dashboard clicks through resolves.

The vocabulary is the real one: auditor names from config/presets and plugins/, reason codes from
intercept/governed/reason_families.py, the risk numbers from consume_plane/plugins/trajectory_risk.py.
"""
from __future__ import annotations

import time
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

SESSION = "sess_onb_APP0003"
POLICY = "9f86d081884c"  # the gateway reports the first 12 characters of the policy hash
POLICY_HASH = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
FEED = "local-signatures-v1"
MODEL = "gpt-4.1-mini"
AGENTS = ("onboarding-agent", "aml-agent", "kyc-review-agent")
T0 = datetime(2026, 10, 3, 15, 40, tzinfo=timezone.utc)

PERIOD = 75   # seconds between session starts
WINDOW = 40   # sessions kept, newest first
BUDGET = {"tokens": 20000, "tool_calls": 30}  # config/presets/standard.json
DAILY_COST_LIMIT = 5.0
PRICE_IN, PRICE_OUT = 0.4, 1.6  # USD per million tokens, an estimate like the usage itself

SEVERITIES = ["info", "low", "medium", "high", "critical"]
STATUS_FOR_DECISION = {"ALLOW": "completed", "ALERT": "completed", "REDACT": "redacted",
                       "REQUIRE_APPROVAL": "pending_approval", "BLOCK": "blocked"}
SEVERITY_FOR_DECISION = {"BLOCK": "high", "ALERT": "medium", "REQUIRE_APPROVAL": "medium", "REDACT": "low"}

# (auditor, method, typical latency in ms, control family). Every deterministic auditor runs on every
# gateway-evaluated action; the semantic guard runs on prompts unless a deterministic one blocked.
AUDITORS = (
    ("policy", "deterministic", 0.12, "other"),
    ("signature-scanner", "deterministic", 0.30, "exploit_signature"),
    ("pattern-match", "deterministic", 0.18, "injection"),
    ("privacy-scanner", "deterministic", 0.42, "pii"),
    ("secret-scanner", "deterministic", 0.35, "secrets"),
    ("domain-blocklist", "deterministic", 0.06, "other"),
    ("velocity-guard", "deterministic", 0.04, "other"),
    ("budget-guard", "deterministic", 0.05, "budget"),
    ("semantic-guard", "semantic", 150.0, "injection"),
)
METHOD = {name: method for name, method, _, _ in AUDITORS}
FAMILY = {name: family for name, _, _, family in AUDITORS} | {"trajectory-risk": "trajectory",
                                                              "outcome-verifier": "outcome"}
OWASP = {"signature-scanner": ["LLM05:2025 Improper Output Handling"], "pattern-match": ["LLM01:2025 Prompt Injection"],
         "semantic-guard": ["LLM01:2025 Prompt Injection"], "privacy-scanner": ["LLM02:2025 Sensitive Information Disclosure"],
         "secret-scanner": ["LLM02:2025 Sensitive Information Disclosure"], "budget-guard": ["LLM10:2025 Unbounded Consumption"],
         "velocity-guard": ["LLM10:2025 Unbounded Consumption"], "policy": ["LLM06:2025 Excessive Agency"],
         "trajectory-risk": ["LLM06:2025 Excessive Agency"]}
REASON_TEXT = {
    "SIGNATURE_MATCH": "The request matched a known exploit signature.",
    "PRIVACY_MATCH": "The request carried personal data or a secret.",
    "AUDITOR_BLOCK": "The semantic guard classified the prompt as an instruction override.",
    "DOMAIN_BLOCKLISTED": "The target host is on the egress blocklist.",
    "BUDGET_EXHAUSTED": "The session budget would be exceeded.",
    "APPROVAL_REQUIRED": "The policy requires a human approval for this tool.",
    "RESOURCE_OUT_OF_SCOPE": "The call targets a case outside the Task Contract.",
    "TOOL_RATE_EXCEEDED": "Tool calls arrived faster than the configured rate.",
    "OUT_OF_CONTRACT_TOOL": "The agent called a tool that is not in its Task Contract.",
    "MISSING_PREREQUISITE": "A required earlier step, such as sanctions screening, had not run.",
    "SCREENING_EVIDENCE_MISSING": "Sanctions screening evidence for the applicant was not found.",
}

# Trajectory risk, the defaults of consume_plane/plugins/trajectory_risk.py
BASE_P = 0.02
SIGNAL_WEIGHTS = {"gateway_blocked": 0.15, "gateway_redacted": 0.05, "gateway_alert": 0.10, "tool_error": 0.05,
                  "out_of_contract_tool": 0.30, "out_of_scope_target": 0.25, "repeated_side_effect": 0.40,
                  "repeated_read": 0.10, "missing_prerequisite": 0.50, "untrusted_external_content": 0.10,
                  "budget_pressure": 0.10}
CONSEQUENCE = {"read": 1.0, "write": 5.0, "irreversible": 10.0}
TOOL_CONSEQUENCE = {"create_client": 8.0, "reject_application": 4.0, "escalate_edd": 2.0, "request_more_docs": 2.0,
                    "delete_client": 10.0, "send_email": 6.0, "run_code": 9.0, "load_risk_model": 7.0}
LEVELS = (("critical", 15.0), ("high", 8.0), ("medium", 3.0), ("low", 0.0))
SIDE_EFFECT = {"create_client": "write", "reject_application": "write", "request_more_docs": "write",
               "send_email": "write", "run_code": "irreversible", "delete_client": "irreversible"}
ADJUSTMENT = {"high": ("REQUIRE_APPROVAL_FOR", 900), "critical": ("HALT_SESSION", 1800)}


def P(decision: str = "ALLOW", auditor: str | None = None, rule: str | None = None, reason: str | None = None):
    return ("prompt", MODEL, decision, auditor, rule, reason, ())


def T(name: str, decision: str = "ALLOW", auditor: str | None = None, rule: str | None = None,
      reason: str | None = None, signals: tuple[str, ...] = ()):
    return ("tool_use", name, decision, auditor, rule, reason, signals)


_READS = [P(), T("read_application"), T("read_documents")]
SCENARIOS = {
    "clean": [*_READS, P(), T("extract_fields"), T("check_registry"), T("screen_sanctions"), P(),
              T("compute_risk"), T("create_client"), T("send_email")],
    "redact": [*_READS, P(), T("extract_fields", "REDACT", "privacy-scanner", "privacy.pesel", "PRIVACY_MATCH",
                               ("gateway_redacted",)),
               T("check_registry"), T("screen_sanctions"), P(), T("compute_risk"), T("create_client")],
    "held": [*_READS, T("extract_fields"), T("screen_sanctions"), P(), T("compute_risk"),
             T("create_client", "REQUIRE_APPROVAL", "policy", "approval.create_client", "APPROVAL_REQUIRED"),
             T("request_more_docs")],
    "injection": [*_READS, T("fetch_url"), P("BLOCK", "semantic-guard", "semantic.prompt_injection", "AUDITOR_BLOCK"),
                  P(), T("extract_fields"),
                  T("send_email", "BLOCK", "signature-scanner", "regex.pattern.0", "SIGNATURE_MATCH", ("gateway_blocked",)),
                  T("screen_sanctions"), T("create_client"), T("send_email")],
    "secret": [*_READS, T("send_email", "BLOCK", "secret-scanner", "secret.api_key", "PRIVACY_MATCH", ("gateway_blocked",)),
               P(), T("extract_fields"), T("reject_application")],
    "egress": [*_READS, T("fetch_url", "BLOCK", "domain-blocklist", "domain.blocklist", "DOMAIN_BLOCKLISTED",
                          ("gateway_blocked",)),
               P(), T("extract_fields"), T("create_client", signals=("missing_prerequisite",)), T("send_email")],
    "budget": [*_READS, T("read_documents"),
               T("read_documents", "ALERT", "velocity-guard", "velocity.burst", "TOOL_RATE_EXCEEDED",
                 ("gateway_alert", "repeated_read")),
               P(), T("extract_fields"), P("BLOCK", "budget-guard", "budget.budget_exhausted", "BUDGET_EXHAUSTED")],
    # The worked example of docs/trajectory-risk-model.md: ends halted at critical.
    "risky": [P(), T("read_application"),
              T("read_application", "ALERT", "policy", "scope.target", "RESOURCE_OUT_OF_SCOPE",
                ("out_of_scope_target", "gateway_alert")),
              T("read_documents"), T("extract_fields"), P(), T("compute_risk"),
              T("create_client", signals=("missing_prerequisite",)),
              T("create_client", signals=("missing_prerequisite", "repeated_side_effect")),
              T("run_code", "BLOCK", "signature-scanner", "regex.pattern.4", "SIGNATURE_MATCH", ("gateway_blocked",)),
              T("delete_client", signals=("out_of_contract_tool", "out_of_scope_target"))],
}
MIX = ("clean", "redact", "clean", "held", "clean", "injection", "clean", "redact", "secret", "clean",
       "egress", "clean", "held", "clean", "budget", "redact", "clean", "risky", "clean", "injection")


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def now() -> str:
    return iso(datetime.now(timezone.utc))


def usage(input_tokens: int = 0, output_tokens: int = 0, cost: float = 0.0,
          latency: float = 0.0, source: str = "estimated") -> dict[str, Any]:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens, "cost_usd": round(cost, 6),
            "latency_ms": round(latency, 1), "source": source}


def total_usage(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return usage(sum(s["usage"]["input_tokens"] for s in steps), sum(s["usage"]["output_tokens"] for s in steps),
                 sum(s["usage"]["cost_usd"] for s in steps), sum(s["usage"]["latency_ms"] for s in steps))


def event_id(session_id: str, seq: int) -> str:
    return f"evt_{session_id}_{seq:04d}"


def _level(loss: float) -> str:
    return next(name for name, threshold in LEVELS if loss >= threshold)


def _max_severity(items: list[dict[str, Any]]) -> str | None:
    return max((d["severity"] for d in items), key=SEVERITIES.index) if items else None


def _chain(kind: str, decision: str, auditor: str | None, rule: str | None, h: int) -> list[dict[str, Any]]:
    """Every auditor that ran on one action, in order, as Event Envelope v2.1 auditor_decisions."""
    blocked_early = decision == "BLOCK" and METHOD.get(auditor) == "deterministic"
    out = []
    for i, (name, method, base, _) in enumerate(AUDITORS):
        if method == "semantic" and (kind != "prompt" or blocked_early):
            continue
        acted = name == auditor
        out.append({"auditor": name, "decision": decision if acted else "ALLOW", "rule_id": rule if acted else None,
                    "latency_ms": round(base * (0.6 + ((h >> i) % 100) / 80), 3)})
    return out


def _build(session_id: str, k: int, start: datetime, until: datetime | None, plan_name: str) -> dict[str, Any]:
    """One session with the steps whose timestamp is not after ``until`` (all of them when None)."""
    n = k % 10000
    ids = {"session_id": session_id, "run_id": f"run_{n:04d}", "agent_id": AGENTS[k % len(AGENTS)],
           "case_id": f"APP-{n:04d}"}
    session: dict[str, Any] = {**ids, "contract_id": f"contract_APP{n:04d}_v1", "state": "active", "start": start,
                               "steps": [], "auditors": {}, "parameters": {}, "detections": [], "interventions": []}
    steps, t = session["steps"], start
    survive, loss, max_c, level, signals = 1 - BASE_P, 0.0, 0.0, "low", []

    def add(kind, name, decision=None, auditor=None, rule=None, reason=None, at=None) -> dict[str, Any]:
        seq = len(steps)
        eid, h = event_id(session_id, seq), zlib.crc32(f"{session_id}:{seq}".encode())
        status = STATUS_FOR_DECISION[decision] if decision else "completed"
        executed = status in ("completed", "redacted")
        chain = _chain(kind, decision, auditor, rule, h) if decision else []
        if kind == "prompt" and executed:
            tokens_in, tokens_out = 700 + 90 * seq + h % 400, 90 + h % 220
            use = usage(tokens_in, tokens_out, (tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 1e6, 380 + h % 520)
        else:
            use = usage(latency=30 + h % 260 if kind == "tool_use" and executed else 0.0)
        step = {
            "event_id": eid, "action_id": f"act_{seq}" if kind in ("prompt", "tool_use") else None, "seq": seq,
            "run_index": 2 * seq, "ts": iso(at or t), **ids, "kind": kind, "name": name,
            "side_effect": SIDE_EFFECT.get(name, "read") if kind == "tool_use" else None,
            "status": status, "executed": executed, "decision": decision,
            "triggered_rules": [{k: a[k] for k in ("auditor", "decision", "rule_id")} for a in chain
                                if a["decision"] != "ALLOW"],
            "reason_code": reason, "policy_version": POLICY, "usage": use,
            "interception_overhead_ms": round(sum(a["latency_ms"] for a in chain), 2) if chain else None,
            "detections": [], "max_severity": None, "fault_injected": False,
        }
        steps.append(step)
        session["auditors"][eid] = chain
        if decision in SEVERITY_FOR_DECISION:
            detect(step, f"gw_{eid}", rule or auditor, reason, SEVERITY_FOR_DECISION[decision], "gateway", auditor,
                   decision, [eid], confidence=0.93 if METHOD.get(auditor) == "semantic" else None)
        return step

    def detect(step, detection_id, name, reason, severity, source, detector, action_taken, evidence, **extra):
        session["detections"].append({
            "detection_id": detection_id, "name": name, "ts": step["ts"], "reason": reason,
            "reason_text": REASON_TEXT.get(reason), "severity": severity, "source": source, "detector": detector,
            "detector_version": "1.0.0" if source != "gateway" else None, "method": METHOD.get(detector, "deterministic"),
            "confidence": None, "action_taken": action_taken, "trigger_event_id": step["event_id"],
            "evidence_event_ids": evidence, "owasp": OWASP.get(detector, []), "details": {}, **ids,
            "policy_version": POLICY, **extra})
        step["detections"].append({"detection_id": detection_id, "name": name, "severity": severity})
        step["max_severity"] = _max_severity(step["detections"])

    rows = [("session", "started", None, None, None, None, ()), *SCENARIOS[plan_name],
            ("session", "ended", None, None, None, None, ())]
    for i, (kind, name, decision, auditor, rule, reason, own) in enumerate(rows):
        if i:
            t += timedelta(seconds=3 + zlib.crc32(f"{session_id}/{i}".encode()) % 7)
        if until is not None and t > until:
            break
        step = add(kind, name, decision, auditor, rule, reason)
        if kind == "session":
            if name == "ended":
                session["state"] = "ended"
            continue
        if kind != "tool_use":
            continue
        scoped = "out_of_scope_target" in own
        session["parameters"][step["event_id"]] = (
            {"host": "pastebin.com" if decision == "BLOCK" else "intranet.bank.example"} if name == "fetch_url"
            else {"application_id": f"APP-{(n - 1) % 10000:04d}" if scoped else ids["case_id"]})
        # Trajectory risk: this step's signals raise P for it and for every later step; only an
        # executed step adds P x consequence to the expected loss.
        for s in own:
            survive *= 1 - SIGNAL_WEIGHTS[s]
            signals.append((s, step["event_id"]))
        if step["executed"]:
            c = TOOL_CONSEQUENCE.get(name, CONSEQUENCE[step["side_effect"]])
            loss, max_c = loss + (1 - survive) * c, max(max_c, c)
            if name == "fetch_url":
                survive *= 1 - SIGNAL_WEIGHTS["untrusted_external_content"]
                signals.append(("untrusted_external_content", step["event_id"]))
        if dict(LEVELS)[_level(loss)] > dict(LEVELS)[level]:  # report only when the session enters a higher level
            level = _level(loss)
            adjustment, ttl = ADJUSTMENT.get(level, ("NONE", 0))
            counts = Counter(s for s, _ in signals)
            top = counts.most_common(1)[0][0].upper() if counts else None
            detect(step, f"fnd_{session_id}_{level}", f"risk.trajectory_{level}", top, level, "finding",
                   "trajectory-risk", adjustment, list(dict.fromkeys(e for _, e in signals)) or [step["event_id"]],
                   details={"expected_loss": round(loss, 4), "failure_probability": round(1 - survive, 4),
                            "signals": dict(counts)})
            if adjustment != "NONE":
                applied = add("control", "adjustment_applied", at=t + timedelta(seconds=1))
                session["interventions"].append({
                    "signal_id": f"sig_{zlib.crc32(applied['event_id'].encode()):08x}", "ts": applied["ts"],
                    "session_id": session_id, "action": adjustment, "tools": ["*"] if level == "high" else [],
                    "scope": "session", "ttl_seconds": ttl, "expires_at": iso(t + timedelta(seconds=ttl + 1)),
                    "source_plugin": "trajectory-risk", "trigger_event_id": step["event_id"], "applied": True,
                    "applied_event_id": applied["event_id"], "active": True, "reason": f"RISK_{level.upper()}"})
            if level == "critical":
                session["state"] = "halted"
                break
    session["risk"] = {"level": level, "expected_loss": round(loss, 4), "failure_probability": round(1 - survive, 4),
                       "max_consequence": max_c or None,
                       "finding_id": f"fnd_{session_id}_{level}" if level != "low" else None, "source": "trajectory-risk"}
    if session["state"] == "halted":
        detect(steps[-1], f"ver_{session_id}", "verification.incomplete", "SCREENING_EVIDENCE_MISSING", "high",
               "verification", "outcome-verifier", "NONE", [steps[-1]["event_id"]])
    return session


_cache: tuple[int, list[dict[str, Any]]] = (0, [])
clock = time.time  # tests replace this to freeze the window


def _window() -> list[dict[str, Any]]:
    """The rolling window of sessions, newest first. Rebuilt at most once a second."""
    global _cache
    tick = int(clock())
    if _cache[0] != tick:
        at, newest = datetime.fromtimestamp(tick, timezone.utc), tick // PERIOD
        _cache = (tick, [_build(f"sess_onb_APP{k % 10000:04d}", k, datetime.fromtimestamp(k * PERIOD, timezone.utc),
                                at, MIX[k % len(MIX)]) for k in range(newest, newest - WINDOW, -1)])
    return _cache[1]


def _session(session_id: str) -> dict[str, Any]:
    found = next((s for s in _window() if s["session_id"] == session_id), None)
    return found or _build(session_id, 3, T0, None, "risky")


def _split(event_id_value: str) -> tuple[str, int]:
    if event_id_value.startswith("evt_") and event_id_value[-4:].isdigit():
        return event_id_value[4:-5] or SESSION, int(event_id_value[-4:])
    return SESSION, 0


def _in(ts: str, since: str | None, until: str | None) -> bool:
    return (since is None or ts >= since) and (until is None or ts < until)


# --- actions and trajectories -------------------------------------------------------------------

def steps(session_id: str) -> list[dict[str, Any]]:
    return _session(session_id)["steps"]


def all_steps() -> list[dict[str, Any]]:
    """Every action of every session, newest first."""
    return sorted((s for session in _window() for s in session["steps"]),
                  key=lambda s: (s["ts"], s["session_id"], s["seq"]), reverse=True)


def action(event_id_value: str) -> dict[str, Any]:
    session_id, seq = _split(event_id_value)
    session = _session(session_id)
    rows = session["steps"]
    summary = {**rows[min(seq, len(rows) - 1)], "event_id": event_id_value}
    seq, kind, name, eid = summary["seq"], summary["kind"], summary["name"], rows[min(seq, len(rows) - 1)]["event_id"]
    if kind == "prompt":
        details = {"model": name, "provider": "openai", "messages": [], "completion": None,
                   "tool_calls_requested": [], "stop_reason": "end_turn" if summary["executed"] else None}
    elif kind == "tool_use":
        details = {"name": name, "side_effect": summary["side_effect"], "transport": "inproc",
                   "parameters": session["parameters"].get(eid, {}), "result": None, "error": None}
    elif kind == "session":
        details = {"phase": name, "contract_id": session["contract_id"], "policy_version": POLICY,
                   "end_reason": "agent_finished" if name == "ended" else None}
    else:
        details = {"change": name, "policy_version": POLICY,
                   "signal_id": next((i["signal_id"] for i in session["interventions"]
                                      if i["applied_event_id"] == eid), None)}
    event = {
        "schema_version": "2.1", "event_id": event_id_value, "action_id": summary["action_id"], "seq": seq,
        "ts": summary["ts"], "run_id": summary["run_id"], "trace_id": summary["run_id"], "session_id": session_id,
        "case_id": summary["case_id"], "agent_id": summary["agent_id"], "step_id": summary["run_index"],
        "parent_span_id": None, "status": summary["status"],
        "action_type": {"prompt": "llm_call", "tool_use": "tool_call"}.get(kind, kind),
        "action_details": details,
        "metrics": {k: summary["usage"][k] for k in ("input_tokens", "output_tokens", "latency_ms", "cost_usd")},
        "fault_injected": False,
    }
    if summary["decision"] is not None:
        event["interception_metadata"] = {
            "final_decision": summary["decision"], "policy_version": POLICY,
            "auditor_decisions": session["auditors"][eid],
            "interception_overhead_ms": summary["interception_overhead_ms"]}
    related = []
    if kind == "tool_use" and summary["side_effect"] != "read":
        related.append({"event_id": f"{event_id_value}_intent", "status": "pending", "ts": summary["ts"],
                        "relation": "intent"})
    refs = {d["detection_id"] for d in summary["detections"]}
    return {
        "summary": summary, "event": event,
        "context": {"contract_id": session["contract_id"], "principal_id": "op_1", "policy_hash": POLICY_HASH,
                    "feed_version": FEED, "approval_id": None, "intervention_id": None, "effect_receipt_id": None,
                    "semantic_model": MODEL if any(a["auditor"] == "semantic-guard" for a in session["auditors"][eid]) else None,
                    "semantic_model_version": None,
                    "reserved_usage": {"reserved_tokens": 2048} if kind == "prompt" else {}},
        "related": related,
        "previous_event_id": event_id(session_id, seq - 1) if seq > 0 else None,
        "next_event_id": event_id(session_id, seq + 1) if seq < len(rows) - 1 else None,
        "detections": [d for d in session["detections"] if d["detection_id"] in refs],
    }


def _segment(session: dict[str, Any], view: str) -> dict[str, Any]:
    rows = [action(s["event_id"]) for s in session["steps"]] if view == "full" else session["steps"]
    summary = session_summary(session)
    return {**{k: summary[k] for k in ("session_id", "run_id", "agent_id", "case_id", "contract_id", "state",
                                       "started_at", "ended_at", "end_reason")}, "steps": rows, "gaps": []}


def trajectory(scope: str, scope_id: str, view: str) -> dict[str, Any]:
    key = {"run": "run_id", "case": "case_id", "agent": "agent_id"}.get(scope)
    sessions_ = [s for s in _window() if key and s[key] == scope_id] or [_session(scope_id if scope == "session" else SESSION)]
    sessions_ = sessions_[:1] if scope in ("session", "run") else sessions_
    rows = [s for session in sessions_ for s in session["steps"]]
    worst = max(sessions_, key=lambda s: s["risk"]["expected_loss"])
    count = Counter(s["status"] for s in rows)
    return {
        "scope": scope, "id": scope_id, "ordering": {"session": "seq", "run": "run_index"}.get(scope, "ts"),
        "segments": [_segment(s, view) for s in sessions_],
        "totals": {"steps": len(rows), "executed": sum(s["executed"] for s in rows), "blocked": count["blocked"],
                   "redacted": count["redacted"], "pending_approval": count["pending_approval"], "failed": count["failed"],
                   "detections": sum(len(s["detections"]) for s in sessions_),
                   "max_severity": _max_severity([d for s in sessions_ for d in s["detections"]]),
                   "usage": total_usage(rows)},
        "risk": worst["risk"],
        "verification_status": verification(sessions_[0]["session_id"])["verification_status"]
        if scope in ("session", "run") else None,
    }


# --- detections -----------------------------------------------------------------------------------

def detection(detection_id: str) -> dict[str, Any]:
    for session in _window():
        for d in session["detections"]:
            if d["detection_id"] == detection_id:
                return d
    example = _session(SESSION)["detections"]
    return {**next((d for d in example if d["detection_id"][:4] == detection_id[:4]), example[0]),
            "detection_id": detection_id}


def detections(session_id: str | None = None) -> list[dict[str, Any]]:
    sessions_ = [_session(session_id)] if session_id else _window()
    return sorted((d for s in sessions_ for d in s["detections"]), key=lambda d: d["ts"], reverse=True)


def catalog() -> dict[str, Any]:
    def entry(name, title, description, severity, reason, detector):
        return {"name": name, "title": title, "description": description, "default_severity": severity,
                "reasons": {reason: REASON_TEXT[reason]}, "owasp": OWASP.get(detector, []),
                "control_family": FAMILY[detector]}
    return {"catalog_version": "c2", "entries": [
        entry("risk.trajectory_critical", "Trajectory risk critical",
              "Cumulative expected loss of the session crossed the critical threshold.", "critical",
              "OUT_OF_CONTRACT_TOOL", "trajectory-risk"),
        entry("risk.trajectory_high", "Trajectory risk high",
              "Cumulative expected loss of the session crossed the high threshold.", "high",
              "MISSING_PREREQUISITE", "trajectory-risk"),
        entry("regex.pattern.4", "Known exploit signature",
              "The request matched an entry of the historical exploit signature feed.", "high",
              "SIGNATURE_MATCH", "signature-scanner"),
        entry("semantic.prompt_injection", "Prompt injection",
              "The semantic guard judged the prompt to carry an instruction override.", "high",
              "AUDITOR_BLOCK", "semantic-guard"),
        entry("privacy.pesel", "Personal data", "A PESEL or IBAN was masked before the call was forwarded.", "low",
              "PRIVACY_MATCH", "privacy-scanner"),
        entry("secret.api_key", "Secret", "An API key or private key was found in the request.", "high",
              "PRIVACY_MATCH", "secret-scanner"),
        entry("budget.budget_exhausted", "Budget exhausted", "The call would cross the session budget.", "high",
              "BUDGET_EXHAUSTED", "budget-guard"),
        entry("verification.incomplete", "Outcome not verified",
              "Independent verification could not confirm every postcondition.", "high",
              "SCREENING_EVIDENCE_MISSING", "outcome-verifier"),
    ]}


# --- sessions ---------------------------------------------------------------------------------------

def contract(session_id: str) -> dict[str, Any]:
    session = _session(session_id)
    return {"contract_id": session["contract_id"], "role": "kyc_onboarding", "target_ids": [session["case_id"]],
            "allowed_tools": ["read_application", "read_documents", "extract_fields", "check_registry",
                              "screen_sanctions", "compute_risk", "create_client", "request_more_docs",
                              "reject_application", "send_email", "fetch_url"],
            "postconditions": ["ONB-P1", "ONB-P2", "ONB-P6"], "budget": {**BUDGET, "cost_usd": None},
            "policy_version": POLICY, "policy_hash": POLICY_HASH, "feed_version": FEED}


def interventions(session_id: str | None = None) -> list[dict[str, Any]]:
    sessions_ = [_session(session_id)] if session_id else _window()
    return sorted((i for s in sessions_ for i in s["interventions"]), key=lambda i: i["ts"], reverse=True)


def verification(session_id: str) -> dict[str, Any]:
    session = _session(session_id)
    last = session["steps"][-1]["ts"]
    if session["state"] == "active":
        return {"session_id": session_id, "verification_status": None, "verified_at": None, "checks": []}
    if session["state"] == "halted":
        return {"session_id": session_id, "verification_status": "VERIFICATION_INCOMPLETE", "verified_at": last,
                "checks": [{"id": "KYC-SCREENING-EVIDENCE", "status": "INCOMPLETE",
                            "detail": "SCREENING_EVIDENCE_MISSING", "evidence_source": None},
                           {"id": "ONB-P2", "status": "PASS", "detail": None, "evidence_source": "bank_snapshot"}]}
    return {"session_id": session_id, "verification_status": "VERIFIED_SUCCESS", "verified_at": last,
            "checks": [{"id": c, "status": "PASS", "detail": None, "evidence_source": "bank_snapshot"}
                       for c in ("ONB-P1", "ONB-P2", "ONB-P6")]}


def session_summary(session: dict[str, Any]) -> dict[str, Any]:
    rows, ended = session["steps"], session["state"] == "ended"
    return {**{k: session[k] for k in ("session_id", "run_id", "agent_id", "case_id", "contract_id")},
            "policy_version": POLICY, "state": session["state"], "started_at": rows[0]["ts"],
            "ended_at": rows[-1]["ts"] if ended else None, "end_reason": "agent_finished" if ended else None,
            "action_count": len(rows), "blocked_count": sum(s["status"] == "blocked" for s in rows),
            "detection_count": len(session["detections"]), "max_severity": _max_severity(session["detections"]),
            "risk_level": session["risk"]["level"],
            "verification_status": verification(session["session_id"])["verification_status"] if session["state"] != "active" else None,
            "usage": total_usage(rows)}


def sessions() -> list[dict[str, Any]]:
    return [session_summary(s) for s in _window()]


def session_detail(session_id: str) -> dict[str, Any]:
    session = _session(session_id)
    severity = Counter(d["severity"] for d in session["detections"])
    return {**session_summary(session), "contract": contract(session_id),
            "run": {"lifecycle": "OPEN" if session["state"] == "active" else "SEALED",
                    "sealed_at": None if session["state"] == "active" else session["steps"][-1]["ts"],
                    "expired_at": None, "total_events": len(session["steps"])},
            "detections_by_severity": {s: severity[s] for s in SEVERITIES},
            "active_interventions": session["interventions"], "verification": verification(session_id)}


def _budget(resource: str, limit: float, used: float, scope: str) -> dict[str, Any]:
    return {"resource": resource, "limit": limit, "used": used, "reserved": 0, "remaining": max(limit - used, 0),
            "utilisation": round(used / limit, 3), "exceeded": used > limit, "scope": scope}


def session_usage(session_id: str) -> dict[str, Any]:
    rows = _session(session_id)["steps"]
    models = [s for s in rows if s["kind"] == "prompt"]
    tools = [s for s in rows if s["kind"] == "tool_use"]
    total = total_usage(rows)
    by_tool: dict[str, dict[str, Any]] = {}
    for s in tools:
        row = by_tool.setdefault(s["name"], {"tool": s["name"], "total": 0, "blocked": 0, "side_effect": s["side_effect"]})
        row["total"] += 1
        row["blocked"] += s["status"] == "blocked"
    first, last = rows[0]["ts"], rows[-1]["ts"]
    return {
        "session_id": session_id,
        "window": {"first_ts": first, "last_ts": last,
                   "duration_s": (datetime.fromisoformat(last[:-1]) - datetime.fromisoformat(first[:-1])).total_seconds()},
        "usage": total,
        "model_calls": {"total": len(models), "completed": sum(s["executed"] for s in models),
                        "blocked": sum(s["status"] == "blocked" for s in models), "failed": 0,
                        "by_model": dict(Counter(s["name"] for s in models))},
        "tool_calls": {"total": len(tools), "executed": sum(s["executed"] for s in tools),
                       "blocked": sum(s["status"] == "blocked" for s in tools),
                       "pending_approval": sum(s["status"] == "pending_approval" for s in tools), "failed": 0,
                       "by_tool": list(by_tool.values())},
        "egress_calls": {"total": 0, "blocked": 0, "by_host": {}},
        "latency_ms": {"backend_sum": total["latency_ms"], **{f"action_{k}": v for k, v in _quantiles(
            [s["usage"]["latency_ms"] for s in rows if s["executed"] and s["decision"]]).items() if k != "p99"},
            **{f"interception_overhead_{k}": v for k, v in _quantiles(
                [s["interception_overhead_ms"] for s in rows if s["decision"]]).items() if k != "p99"}},
        "budgets": [_budget("tokens", BUDGET["tokens"], total["total_tokens"], "session"),
                    _budget("tool_calls", BUDGET["tool_calls"], len(tools), "session")],
        "budget_blocks": sum(s["reason_code"] == "BUDGET_EXHAUSTED" for s in rows),
    }


# --- aggregates -------------------------------------------------------------------------------------

def _quantiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def q(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(p * len(ordered)))], 2) if ordered else 0.0
    return {"p50": q(0.5), "p95": q(0.95), "p99": q(0.99)}


def _selected(since: str | None, until: str | None, session_id: str | None = None, agent_id: str | None = None,
              case_id: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sessions that match the filters and their actions inside the time window."""
    sessions_ = [s for s in _window() if (session_id is None or s["session_id"] == session_id)
                 and (agent_id is None or s["agent_id"] == agent_id) and (case_id is None or s["case_id"] == case_id)]
    rows = [(s, x) for s in sessions_ for x in s["steps"] if _in(x["ts"], since, until)]
    return [s for s in sessions_ if any(owner is s for owner, _ in rows)], [x for _, x in rows]


def usage_report(group_by: str, since: str, until: str, agent_id: str | None = None,
                 case_id: str | None = None) -> dict[str, Any]:
    _, rows = _selected(since, until, None, agent_id, case_id)
    key = {"agent": lambda s: s["agent_id"], "session": lambda s: s["session_id"], "case": lambda s: s["case_id"],
           "model": lambda s: s["name"] if s["kind"] == "prompt" else None,
           "tool": lambda s: s["name"] if s["kind"] == "tool_use" else None, "day": lambda s: s["ts"][:10]}[group_by]
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in rows:
        if key(s) is not None:
            groups.setdefault(key(s), []).append(s)
    buckets = [{"key": k, "sessions": len({s["session_id"] for s in g}),
                "model_calls": sum(s["kind"] == "prompt" for s in g), "tool_calls": sum(s["kind"] == "tool_use" for s in g),
                "blocked": sum(s["status"] == "blocked" for s in g), "usage": total_usage(g),
                "budget": _budget("cost_usd", DAILY_COST_LIMIT, total_usage(g)["cost_usd"], "day") if group_by == "day" else None}
               for k, g in groups.items()]
    buckets.sort(key=lambda b: b["key"] if group_by == "day" else -b["usage"]["total_tokens"])
    return {"group_by": group_by, "since": since, "until": until, "totals": total_usage(rows), "buckets": buckets}


def security_overview(since: str, until: str, top: int, session_id: str | None = None,
                      agent_id: str | None = None) -> dict[str, Any]:
    sessions_, rows = _selected(since, until, session_id, agent_id)
    evaluated = [s for s in rows if s["decision"]]
    found = [d for s in sessions_ for d in s["detections"] if _in(d["ts"], since, until)]
    decisions, kinds = Counter(s["decision"] for s in evaluated), Counter(s["kind"] for s in rows)
    names, worst = Counter(d["name"] for d in found), {}
    for d in found:
        worst[d["name"]] = _max_severity([d, {"severity": worst.get(d["name"], "info")}])
    family = Counter(FAMILY.get(d["detector"], "other") for d in found)
    states, verified = Counter(s["state"] for s in sessions_), Counter(
        verification(s["session_id"])["verification_status"] or "none" for s in sessions_)
    signals = [i for s in sessions_ for i in s["interventions"] if _in(i["ts"], since, until)]
    return {
        "since": since, "until": until,
        "actions": {"total": len(rows),
                    "by_decision": {d: decisions[d] for d in ("ALLOW", "BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT")},
                    "by_kind": {k: kinds[k] for k in ("prompt", "tool_use", "egress", "session", "approval", "control")}},
        "block_rate": round(decisions["BLOCK"] / len(evaluated), 3) if evaluated else 0.0,
        "redact_rate": round(decisions["REDACT"] / len(evaluated), 3) if evaluated else 0.0,
        "detections": {"total": len(found),
                       "by_severity": {s: sum(d["severity"] == s for d in found) for s in SEVERITIES},
                       "by_source": {s: sum(d["source"] == s for d in found)
                                     for s in ("gateway", "finding", "alert", "verification")},
                       "by_control_family": {f: family[f] for f in ("secrets", "injection", "pii", "budget",
                                                                    "exploit_signature", "trajectory", "outcome", "other")}},
        "top_detections": [{"name": n, "count": c, "max_severity": worst[n]} for n, c in names.most_common(top)],
        "top_blocked_tools": [{"tool": t, "count": c} for t, c in Counter(
            s["name"] for s in rows if s["kind"] == "tool_use" and s["status"] == "blocked").most_common(top)],
        "sessions": {"total": len(sessions_), "active": states["active"], "halted": states["halted"],
                     "by_risk_level": {l: sum(s["risk"]["level"] == l for s in sessions_)
                                       for l in ("low", "medium", "high", "critical")}},
        "verification": {k: verified[k] for k in ("VERIFIED_SUCCESS", "FAILED_POSTCONDITIONS",
                                                  "VERIFICATION_INCOMPLETE", "none")},
        "interventions": {"proposed": len(signals), "applied": len(signals), "active": sum(i["active"] for i in signals)},
        "interception_overhead_ms": _quantiles([sum(a["latency_ms"] for a in s["auditors"][x["event_id"]]
                                                    if METHOD[a["auditor"]] == "deterministic")
                                                for s in sessions_ for x in s["steps"]
                                                if x["decision"] and _in(x["ts"], since, until)]),
        "policy_versions": [POLICY], "feed_versions": [FEED],
    }


def performance_overview(since: str, until: str, session_id: str | None = None,
                         agent_id: str | None = None) -> dict[str, Any]:
    sessions_, rows = _selected(since, until, session_id, agent_id)
    evaluated = [s for s in rows if s["decision"]]
    chains = {x["event_id"]: s["auditors"][x["event_id"]] for s in sessions_ for x in s["steps"]}
    per_method: dict[str, list[float]] = {"deterministic": [], "semantic": []}
    per_auditor: dict[str, list[dict[str, Any]]] = {name: [] for name, _, _, _ in AUDITORS}
    for s in evaluated:
        for method in per_method:
            ran = [a["latency_ms"] for a in chains[s["event_id"]] if METHOD[a["auditor"]] == method]
            if ran:
                per_method[method].append(sum(ran))
        for a in chains[s["event_id"]]:
            per_auditor[a["auditor"]].append(a)
    prompts = sum(s["kind"] == "prompt" for s in evaluated)
    overhead = sum(s["interception_overhead_ms"] for s in evaluated)
    backend = [s["usage"]["latency_ms"] for s in evaluated if s["executed"]]
    return {
        "since": since, "until": until, "actions_evaluated": len(evaluated),
        "interception_overhead_ms": _quantiles([s["interception_overhead_ms"] for s in evaluated]),
        "by_method": {
            "deterministic": {"runs": len(per_method["deterministic"]), "skipped": 0,
                              **_quantiles(per_method["deterministic"])},
            "semantic": {"runs": len(per_method["semantic"]), "skipped": prompts - len(per_method["semantic"]),
                         **_quantiles(per_method["semantic"])}},
        "by_auditor": [{"auditor": name, "method": method, "runs": len(per_auditor[name]),
                        "acted": sum(a["decision"] != "ALLOW" for a in per_auditor[name]),
                        **{k: v for k, v in _quantiles([a["latency_ms"] for a in per_auditor[name]]).items() if k != "p99"}}
                       for name, method, _, _ in AUDITORS],
        "backend_latency_ms": _quantiles(backend),
        "overhead_share": round(overhead / (overhead + sum(backend)), 3) if backend else None,
    }


def store_stats() -> dict[str, Any]:
    rows = all_steps()
    status = Counter(s["status"] for s in rows)
    return {"total_events": len(rows), "total_alerts": 0,
            "total_findings": sum(d["source"] == "finding" for d in detections()), "total_dlq_records": 0,
            "events_by_type": {"TOOL_CALL": sum(s["kind"] == "tool_use" for s in rows),
                               "LLM_INVOCATION": sum(s["kind"] == "prompt" for s in rows),
                               "SESSION": sum(s["kind"] == "session" for s in rows),
                               "CONTROL": sum(s["kind"] == "control" for s in rows)},
            "events_by_status": {"EXECUTED": status["completed"] + status["redacted"], "BLOCKED": status["blocked"],
                                 "PENDING": status["pending_approval"]},
            "alerts_by_severity": {}, "average_latency_ms": _quantiles(
                [s["interception_overhead_ms"] for s in rows if s["decision"]])["p50"],
            "pending_deliveries": {"trajectory-risk": 0, "outcome-verifier": 0, "live-feed": 0},
            "db_size_bytes": 5242880, "schema_version": 3}
