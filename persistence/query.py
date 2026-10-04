"""Read-only dashboard queries. No schema initialization, writer or agent-content reads.

Each session operation owns one SQLite read transaction. Cross-store scans are bounded;
they are per-store consistent, not an atomic snapshot across independently written files.
"""
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timezone, timedelta
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from persistence.adapters.consumer_v21 import to_consumer_v21
from persistence.models import ActionEventEnvelope, ActionStatus, ActionType
from persistence.privacy import sanitize_event, token, timestamp, number, evidence
from persistence.query_usage import UsageQueriesMixin
from persistence.privacy import decision_projection, sanitize_event, token, timestamp, number
from persistence.schema import CURRENT_SCHEMA_VERSION
from persistence.settings import PersistenceSettings
from persistence.vocabulary import DECISION_FOR_VERDICT, WIRE_STATUS, is_intent

MAX_STORES = 2000  # one store per chat session and none are pruned; 128 was reached within a day of traffic
MAX_ROWS = 100000
MAX_READ_BYTES = 64 * 1024 * 1024
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SEVERITIES = ("info", "low", "medium", "high", "critical")
DECISION_SEVERITY = {"BLOCK": "high", "REQUIRE_APPROVAL": "medium", "ALERT": "medium", "REDACT": "low"}


class QueryError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


class _ReadConnection(sqlite3.Connection):
    """Connection-owned request budget; never mutable state on ReadQueries."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalized_ts(value):
    return timestamp(value) if value else None


def usage(events):
    # Dispatched model requests (including failed requests) are estimates in the current runtime.
    values = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0,
              "latency_ms": 0, "source": "estimated"}
    for event in events:
        if event.action_type != ActionType.LLM_INVOCATION or event.status not in (
                ActionStatus.EXECUTED, ActionStatus.REDACTED, ActionStatus.FAILED):
            continue
        actual = event.context.actual_usage
        for key in ("input_tokens", "output_tokens", "latency_ms"):
            values[key] += actual.get(key, 0)
        values["cost_usd"] += actual.get("actual_cost", 0)
    values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    return values


def cursor_page(items, limit, cursor, *, scope, filters, key):
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    remaining = items
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if set(data) != {"scope", "filter", "last"} or data["scope"] != scope or data["filter"] != fingerprint:
                raise ValueError
            last = data["last"]
            index = next(i for i, item in enumerate(items) if list(key(item)) == last)
            remaining = items[index + 1:]
        except (ValueError, TypeError, KeyError, StopIteration, UnicodeError):
            raise QueryError(400, "invalid_cursor") from None
    result = remaining[:limit]
    more = len(remaining) > limit
    next_cursor = None
    if more:
        encoded = {"scope": scope, "filter": fingerprint, "last": list(key(result[-1]))}
        next_cursor = base64.urlsafe_b64encode(json.dumps(encoded, separators=(",", ":")).encode()).decode().rstrip("=")
    return {"items": result, "next_cursor": next_cursor, "has_more": more}


class ReadQueries(UsageQueriesMixin):
    def __init__(self, evidence_dir):
        self.directory = Path(evidence_dir).resolve() if evidence_dir is not None else None

    def paths(self):
        if self.directory is None or not self.directory.is_dir():
            raise QueryError(503, "store_unavailable")
        paths = sorted(self.directory.glob("*.evidence.db"))
        if len(paths) > MAX_STORES:
            raise QueryError(503, "store_unavailable")
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise QueryError(503, "store_unavailable")
        return paths

    @contextmanager
    def connect(self, path, *, budget=None):
        conn = None
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1,
                                   factory=_ReadConnection)
            conn._budget = budget if budget is not None else {"rows": 0, "bytes": 0}
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=1000")
            deadline = time.monotonic() + 5
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            conn.execute("BEGIN")
            if conn.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
                raise QueryError(503, "store_unavailable")
            yield conn
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, IndexError,
                RecursionError, OverflowError, StopIteration):
            raise QueryError(503, "store_unavailable") from None
        finally:
            if conn is not None:
                conn.close()

    def _rows(self, conn, sql, args=()):
        rows = []
        budget = conn._budget
        for row in conn.execute(sql, args):
            budget["rows"] += 1
            budget["bytes"] += sum(len(cell.encode("utf-8")) for cell in row if isinstance(cell, str))
            if budget["rows"] > MAX_ROWS or budget["bytes"] > MAX_READ_BYTES:
                raise QueryError(503, "store_unavailable")
            rows.append(row)
        return rows

    def _session_path(self, session_id):
        if not ID.fullmatch(session_id):
            raise QueryError(400, "bad_request")
        if self.directory is None or not self.directory.is_dir():
            raise QueryError(503, "store_unavailable")
        wanted = self.directory / f"{session_id}.evidence.db"
        if wanted.is_symlink():
            raise QueryError(503, "store_unavailable")
        if not wanted.exists():
            raise QueryError(404, "not_found")
        if not wanted.is_file():
            raise QueryError(503, "store_unavailable")
        return wanted

    def _iter_bundles(self, session_id=None):
        """One transaction per relevant store, one raw-read budget for the scan.

        Expiration is retained in the bundle so callers can reject only relevant
        scopes, rather than unrelated expired sessions in another store.
        """
        paths = [self._session_path(session_id)] if session_id is not None else self.paths()
        budget = {"rows": 0, "bytes": 0}
        observed_at = datetime.now(timezone.utc)
        seen_sessions = set()
        for path in paths:
            with self.connect(path, budget=budget) as conn:
                if session_id is not None:
                    ids = [session_id]
                else:
                    ids = [row[0] for row in self._rows(conn,
                        "SELECT session_id FROM task_contracts UNION SELECT session_id FROM events "
                        "UNION SELECT session_id FROM consumer_findings UNION SELECT session_id FROM verification_results "
                        "UNION SELECT session_id FROM policy_signals UNION SELECT session_id FROM alerts")]
                for ident in ids:
                    if ident in seen_sessions:
                        raise QueryError(503, "store_unavailable")
                    seen_sessions.add(ident)
                    yield path, self._load_session(conn, ident, allow_expired=True, observed_at=observed_at)

    def health(self):
        try:
            paths = self.paths()
            for path in paths:
                with self.connect(path) as conn:
                    conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
            return 200, {"status": "ok", "schema_version": CURRENT_SCHEMA_VERSION,
                         "read_only": True, "now": now()}
        except QueryError:
            return 503, {"status": "degraded", "schema_version": CURRENT_SCHEMA_VERSION,
                         "read_only": True, "now": now()}

    def stats(self):
        result = {"total_events": 0, "total_alerts": 0, "total_findings": 0, "total_audit_actions": 0,
                  "total_dlq_records": 0, "events_by_type": Counter(), "events_by_status": Counter(),
                  "alerts_by_severity": Counter(), "pending_deliveries": Counter(),
                  "db_size_bytes": 0, "schema_version": CURRENT_SCHEMA_VERSION,
                  "evidence_stores": 0, "average_latency_ms": 0.0}
        latency_sum = 0
        budget = {"rows": 0, "bytes": 0}
        for path in self.paths():
            with self.connect(path, budget=budget) as conn:
                for table, key in (("events", "total_events"), ("alerts", "total_alerts"),
                                   ("consumer_findings", "total_findings"), ("audit_actions", "total_audit_actions"),
                                   ("dead_letter_queue", "total_dlq_records")):
                    result[key] += self._rows(conn, f"SELECT COUNT(*) FROM {table}")[0][0]
                for table, col, key in (("events", "action_type", "events_by_type"),
                                        ("events", "status", "events_by_status"),
                                        ("alerts", "severity", "alerts_by_severity"),
                                        ("outbox", "consumer_name", "pending_deliveries")):
                    result[key].update({token(row[0], required=True): row[1]
                                        for row in self._rows(conn, f"SELECT {col},COUNT(*) FROM {table} GROUP BY {col}")})
                latency_sum += self._rows(conn, "SELECT COALESCE(SUM(total_latency_ms),0) FROM events")[0][0]
            result["db_size_bytes"] += path.stat().st_size
            result["evidence_stores"] += 1
        result["average_latency_ms"] = round(latency_sum / result["total_events"], 3) if result["total_events"] else 0.0
        return result

    def _load_session(self, conn, session_id, *, allow_expired=False, observed_at=None):
        observed_at = observed_at if observed_at is not None else datetime.now(timezone.utc)
        contract_rows = self._rows(conn, "SELECT contract_json FROM task_contracts WHERE session_id=?", (session_id,))
        contract_row = contract_rows[0] if contract_rows else None
        contract = json.loads(contract_row[0]) if contract_row else None
        if contract:
            contract = {key: token(contract.get(key)) for key in ("contract_id", "run_id", "session_id", "principal_id",
                                                                  "agent_id", "case_id", "policy_version", "policy_hash", "feed_version")} | {
                key: [token(value, required=True) for value in contract.get(key, [])]
                for key in ("target_ids", "allowed_tools", "postconditions")
            } | {"budget": {key: number(value) if value is not None else None
                            for key, value in contract.get("budget", {}).items()
                            if key in ("tokens", "tool_calls", "cost_usd")}}
        rows = self._rows(conn, "SELECT rowid,event_id,payload_json FROM events WHERE session_id=? ORDER BY seq IS NULL,seq,rowid", (session_id,))
        events = [sanitize_event(ActionEventEnvelope.from_json(row[2]), allow_unindexed_run=True) for row in rows]
        if any(event.event_id != row[1] for event, row in zip(events, rows)):
            raise ValueError("event identity mismatch")
        rowids = {row[1]: row[0] for row in rows}
        # Legacy enum values have no supported consumer projection. Reject them
        # even if a later list filter would otherwise hide the malformed event.
        for event in events:
            self._summary(event)
            if not is_intent(event):
                to_consumer_v21(event)
        if any(event.session_id != session_id for event in events):
            raise ValueError("event identity mismatch")
        if contract and contract.get("session_id") != session_id:
            raise ValueError("contract identity mismatch")
        findings = []
        for row in self._rows(conn, "SELECT finding_id,payload_json FROM consumer_findings WHERE session_id=? ORDER BY rowid", (session_id,)):
            data = json.loads(row[1])
            if data.get("session_id", session_id) != session_id:
                raise ValueError("finding identity mismatch")
            # Never return free-text summaries or arbitrary details even from a manually edited DB.
            safe = {key: token(data[key]) for key in ("rule_id", "plugin", "plugin_version", "method", "run_id",
                                                     "session_id", "agent_id", "case_id", "trigger_event_id", "policy_version")
                    if data.get(key) is not None}
            if data.get("severity") not in SEVERITIES:
                raise ValueError("invalid finding severity")
            safe["severity"] = data["severity"]
            if data.get("confidence") is not None:
                confidence = number(data["confidence"])
                if confidence > 1:
                    raise ValueError("invalid confidence")
                safe["confidence"] = confidence
            safe["evidence_event_ids"] = [token(value, required=True) for value in data.get("evidence_event_ids", [])]
            safe["finding_id"] = token(row[0], required=True)
            findings.append(safe)
        verification_rows = self._rows(conn, "SELECT payload_json FROM verification_results WHERE session_id=?", (session_id,))
        verification_row = verification_rows[0] if verification_rows else None
        verification = None
        if verification_row:
            data = json.loads(verification_row[0])
            if data.get("verification_status") not in ("VERIFIED_SUCCESS", "FAILED_POSTCONDITIONS", "VERIFICATION_INCOMPLETE"):
                raise ValueError("invalid verification")
            checks = []
            for check in data.get("checks", []):
                if check["status"] not in ("PASS", "FAIL", "INCOMPLETE"):
                    raise ValueError("invalid check")
                checks.append({"id": token(check["id"], required=True), "status": check["status"],
                               "detail": token(check.get("detail")), "evidence_source": token(check.get("evidence_source"))})
            verification = {"session_id": session_id, "verification_status": data["verification_status"],
                            "verified_at": normalized_ts(data.get("verified_at")), "checks": checks}
        run_id = contract.get("run_id") if contract else next((e.context.run_id for e in events if e.context.run_id), None)
        runs = self._rows(conn, "SELECT lifecycle,sealed_at,expired_at FROM audit_runs WHERE run_id=?", (run_id,)) if run_id else []
        run = runs[0] if runs else None
        if run and run["lifecycle"] not in ("ACTIVE", "SEALED", "EXPIRED"):
            raise ValueError("invalid lifecycle")
        if run and run[0] == "EXPIRED" and not allow_expired:
            raise QueryError(410, "evidence_expired")
        bundle = {"session_id": session_id, "contract": contract, "events": events, "findings": findings,
                  "_rowids": rowids,
                  "verification": verification, "run": {"lifecycle": run["lifecycle"],
                  "sealed_at": normalized_ts(run["sealed_at"]), "expired_at": normalized_ts(run["expired_at"])} if run else None}
        interventions = []
        signal_rowids = {}
        for row in self._rows(conn, "SELECT signal_id,signal_json,applied,rowid FROM policy_signals WHERE session_id=? ORDER BY rowid", (session_id,)):
            data = json.loads(row[1])
            if data.get("session_id", session_id) != session_id or data.get("signal_id", row[0]) != row[0]:
                raise ValueError("signal identity mismatch")
            if data["action"] not in ("ALERT", "REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS", "STRICT_MODE", "HALT_SESSION"):
                raise ValueError("invalid signal")
            ts = normalized_ts(data["ts"])
            ttl = data["ttl_seconds"]
            if type(ttl) is not int or not 0 < ttl <= 86400:
                raise ValueError("invalid signal ttl")
            expires = datetime.fromisoformat(ts.replace("Z", "+00:00")) + timedelta(seconds=ttl)
            try:
                reason = token(data.get("reason"))
            except ValueError:
                reason = None
            mods = data.get("policy_modifications", {})
            tools = mods.get("tools", mods.get("blocked_tools", mods.get("require_approval_for", [])))
            safe_tools = ["*" if value == "*" else token(value, required=True) for value in tools]
            applied_event = next((event.event_id for event in events if event.action_details.wire_details.get("signal_id") == row[0]
                                  and event.action_details.wire_details.get("change") == "adjustment_applied"), None)
            interventions.append({"signal_id": token(row[0], required=True), "ts": ts, "session_id": session_id,
                                  "action": data["action"], "tools": safe_tools, "scope": "session", "ttl_seconds": ttl,
                                  "expires_at": expires.isoformat().replace("+00:00", "Z"),
                                  "source_plugin": token(data["source_plugin"], required=True),
                                  "trigger_event_id": token(data["trigger_event_id"], required=True),
                                  "applied": bool(row[2]), "applied_event_id": applied_event,
                                  "active": bool(row[2]) and expires > observed_at, "reason": reason})
            signal_rowids[row[0]] = row[3]
        bundle["interventions"] = interventions
        bundle["_signal_rowids"] = signal_rowids
        alerts = []
        for row in self._rows(conn, "SELECT alert_id,ts,severity,rule,agent_id,case_id,action_taken,evidence_json "
                              "FROM alerts WHERE session_id=? ORDER BY rowid", (session_id,)):
            severity = row[2].lower()
            if severity not in SEVERITIES:
                raise ValueError("invalid alert severity")
            alerts.append({"alert_id": token(row[0], required=True), "ts": timestamp(row[1]),
                           "severity": severity, "rule": token(row[3], required=True),
                           "agent_id": token(row[4], required=True), "case_id": token(row[5]),
                           "session_id": session_id, "action_taken": token(row[6], required=True),
                           "evidence": evidence(json.loads(row[7]))})
        bundle["alerts"] = alerts
        if not contract and not events and not findings and not verification and not interventions and not alerts:
            raise QueryError(404, "not_found")
        peers = [event for event in events if not is_intent(event)]
        bundle["_peers"] = peers
        bundle["_indices"] = {event.event_id: index for index, event in enumerate(peers)}
        bundle["_related"] = {}
        for event in events:
            if event.context.action_id:
                bundle["_related"].setdefault(event.context.action_id, []).append(event)
        if verification and verification["verified_at"] is None:
            ended = [event.ts for event in events if event.action_details.wire_details.get("phase") == "ended"]
            verification["verified_at"] = ended[-1] if ended else None  # documented fallback to session end
        return bundle

    def _summary(self, event):
        intent = is_intent(event)
        details, ctx, meta = event.action_details, event.context, event.interception_metadata
        kinds = {ActionType.TOOL_CALL: "tool_use", ActionType.MCP_TOOL: "tool_use", ActionType.LLM_INVOCATION: "prompt",
                 ActionType.EGRESS_HTTP: "egress", ActionType.SESSION: "session", ActionType.APPROVAL: "approval", ActionType.CONTROL: "control"}
        kind = kinds[event.action_type]
        decision = None if kind in ("session", "approval", "control") else DECISION_FOR_VERDICT[meta.verdict]
        name = details.name
        wire = details.wire_details
        if kind == "prompt": name = wire.get("model", name)
        elif kind == "egress": name = wire.get("host", name)
        elif kind == "session": name = wire.get("phase", name)
        elif kind == "approval": name = wire.get("decision", name)
        elif kind == "control": name = wire.get("change", name)
        rules = [{"auditor": row.auditor_name, "decision": DECISION_FOR_VERDICT[row.verdict], "rule_id": row.rule}
                 for row in meta.auditor_decisions if DECISION_FOR_VERDICT[row.verdict] != "ALLOW"]
        detections = []
        if not intent and decision in DECISION_SEVERITY:
            detections.append({"detection_id": f"gw_{event.event_id}", "name": rules[0]["rule_id"] or rules[0]["auditor"]
                               if rules else ctx.reason_code or "policy", "severity": DECISION_SEVERITY[decision]})
        measured = ctx.actual_usage
        return {"event_id": event.event_id, "action_id": ctx.action_id, "seq": None if intent else event.seq,
                "run_index": ctx.action_index, "ts": event.ts, "session_id": event.session_id, "run_id": ctx.run_id,
                "agent_id": event.agent_id, "case_id": event.case_id, "kind": kind, "name": name,
                "side_effect": details.side_effect if kind == "tool_use" else None,
                "status": "pending" if intent else WIRE_STATUS[event.status],
                "executed": event.status in (ActionStatus.EXECUTED, ActionStatus.REDACTED),
                "decision": decision, "triggered_rules": rules, "reason_code": ctx.reason_code,
                "policy_version": meta.policy_version, "usage": {
                    "input_tokens": measured.get("input_tokens", 0), "output_tokens": measured.get("output_tokens", 0),
                    "total_tokens": measured.get("input_tokens", 0) + measured.get("output_tokens", 0),
                    "cost_usd": measured.get("actual_cost", 0), "latency_ms": measured.get("latency_ms", meta.total_latency_ms),
                    "source": "estimated"}, "interception_overhead_ms": meta.total_latency_ms if decision else None,
                "detections": detections, "max_severity": detections[0]["severity"] if detections else None,
                "fault_injected": meta.fault_injected}

    def _detections(self, bundle):
        if "_detections" in bundle:
            return bundle["_detections"]
        found = []
        events = {event.event_id: event for event in bundle["events"]}
        for event in events.values():
            summary = self._summary(event)
            for ref in summary["detections"]:
                found.append({**ref, "ts": event.ts, "reason": event.context.reason_code, "reason_text": None,
                              "source": "gateway", "detector": summary["triggered_rules"][0]["auditor"] if summary["triggered_rules"] else "policy",
                              "detector_version": None, "method": "deterministic", "confidence": None,
                              "action_taken": summary["decision"], "session_id": event.session_id, "run_id": event.context.run_id,
                              "agent_id": event.agent_id, "case_id": event.case_id, "trigger_event_id": event.event_id,
                              "evidence_event_ids": [event.event_id], "policy_version": summary["policy_version"], "owasp": [], "details": {}})
        for item in bundle["findings"]:
            trigger = events.get(item.get("trigger_event_id"))
            found.append({"detection_id": item["finding_id"], "name": item.get("rule_id", "finding"),
                          "ts": trigger.ts if trigger else None, "reason": None, "reason_text": None,
                          "severity": item["severity"], "source": "finding", "detector": item.get("plugin"),
                          "detector_version": item.get("plugin_version"), "method": item.get("method", "deterministic"),
                          "confidence": item.get("confidence"), "action_taken": "NONE", "session_id": bundle["session_id"],
                          "run_id": item.get("run_id"), "agent_id": item.get("agent_id"), "case_id": item.get("case_id"),
                          "trigger_event_id": item.get("trigger_event_id"), "evidence_event_ids": item["evidence_event_ids"],
                          "policy_version": item.get("policy_version"), "owasp": [], "details": {}})
        bundle["_detections"] = found
        bundle["_detection_refs"] = {}
        for item in found:
            ids = set(item["evidence_event_ids"]) | ({item["trigger_event_id"]} if item["trigger_event_id"] else set())
            for event_id in ids:
                bundle["_detection_refs"].setdefault(event_id, []).append(item)
        return found

    def _action(self, event, bundle):
        summary = self._summary(event)
        wire = to_consumer_v21(event) if not is_intent(event) else None
        # Intents have no v2.1 consumer seq. Expose their sanitized storage projection explicitly.
        if wire is None:
            wire = {"schema_version": "2.0", "event_id": event.event_id, "session_id": event.session_id,
                    "action_id": event.context.action_id, "seq": None, "ts": event.ts,
                    "status": "pending", "action_details": {"name": event.action_details.name,
                    "parameters": event.action_details.parameters, "result": None}, "intent": True}
        self._detections(bundle)
        detections = bundle["_detection_refs"].get(event.event_id, [])
        summary["detections"] = [{key: item[key] for key in ("detection_id", "name", "severity")} for item in detections]
        summary["max_severity"] = max((item["severity"] for item in detections), key=SEVERITIES.index, default=None)
        peers = bundle["_peers"]
        index = bundle["_indices"].get(event.event_id)
        related = [{"event_id": item.event_id, "status": "pending" if is_intent(item) else WIRE_STATUS[item.status],
                    "ts": item.ts, "relation": "intent" if is_intent(item) else "result"}
                   for item in bundle["_related"].get(event.context.action_id, []) if item.event_id != event.event_id
                   and event.context.action_id and item.context.action_id == event.context.action_id]
        ctx = event.context
        return {"summary": summary, "event": wire, "context": {
            key: getattr(ctx, key) for key in ("contract_id", "principal_id", "policy_hash", "feed_version", "approval_id",
                                              "intervention_id", "effect_receipt_id", "semantic_model", "semantic_model_version", "reserved_usage")},
            "related": related, "previous_event_id": peers[index - 1].event_id if index is not None and index > 0 else None,
            "next_event_id": peers[index + 1].event_id if index is not None and index + 1 < len(peers) else None,
            "detections": detections}

    def _session(self, bundle):
        events = [event for event in bundle["events"] if not is_intent(event)]
        first = events[0] if events else None
        contract = bundle["contract"] or {}
        started = [event for event in events if event.action_type == ActionType.SESSION and event.action_details.wire_details.get("phase") == "started"]
        ended = [event for event in events if event.action_type == ActionType.SESSION and event.action_details.wire_details.get("phase") == "ended"]
        detections = self._detections(bundle)
        verification = bundle["verification"]
        return {"session_id": bundle["session_id"], "run_id": contract.get("run_id") or (first.context.run_id if first else None),
                "agent_id": contract.get("agent_id") or (first.agent_id if first else None),
                "case_id": contract.get("case_id") or (first.case_id if first else None),
                "contract_id": contract.get("contract_id"), "policy_version": contract.get("policy_version"),
                "state": "ended" if ended else ("halted" if any(item["applied"] and item["action"] == "HALT_SESSION"
                                                                          for item in bundle["interventions"]) else "active"),
                "started_at": started[0].ts if started else first.ts if first else None,
                "ended_at": ended[-1].ts if ended else None,
                "end_reason": ended[-1].action_details.wire_details.get("end_reason") if ended else None,
                "action_count": len(events), "blocked_count": sum(self._summary(event)["decision"] == "BLOCK" for event in events),
                "detection_count": len(detections), "max_severity": max((item["severity"] for item in detections), key=SEVERITIES.index, default=None),
                "risk_level": max((item["severity"] for item in bundle["findings"] if item.get("plugin") == "trajectory-risk"),
                                  key=SEVERITIES.index, default=None),
                "verification_status": verification["verification_status"] if verification else None, "usage": usage(events)}

    def session_detail(self, session_id):
        with self.connect(self._session_path(session_id)) as conn:
            bundle = self._load_session(conn, session_id)
            return self._detail(bundle)

    def _detail(self, bundle):
        contract = bundle["contract"]
        safe_contract = None
        if contract:
            safe_contract = {key: contract.get(key) for key in ("contract_id", "role", "target_ids", "allowed_tools",
                                                               "postconditions", "budget", "policy_version", "policy_hash", "feed_version")}
            # No objective, principal credentials or arbitrary config/prose leaves the store.
            safe_contract["role"] = "kyc_onboarding" if contract.get("agent_id") == "onboarding-agent" else None
        run = bundle["run"]
        return {**self._session(bundle), "contract": safe_contract,
                "run": {**run, "total_events": len(bundle["events"])} if run else None,
                "detections_by_severity": {level: sum(item["severity"] == level for item in self._detections(bundle)) for level in SEVERITIES},
                "active_interventions": [item for item in bundle["interventions"] if item["active"]],
                "verification": bundle["verification"]}

    def verification(self, session_id):
        with self.connect(self._session_path(session_id)) as conn:
            bundle = self._load_session(conn, session_id)
            return bundle["verification"] or {"session_id": session_id, "verification_status": None, "verified_at": None, "checks": []}

    def sessions(self, *, limit=100, cursor=None, **filters):
        result = []
        for path, bundle in self._iter_bundles():
            summary = self._session(bundle)
            if any(value is not None and summary.get(key) != value for key, value in filters.items()
                   if key in ("agent_id", "case_id", "run_id", "state")):
                continue
            expected = filters.get("verification_status")
            if expected and summary["verification_status"] != (None if expected == "none" else expected):
                continue
            minimum = filters.get("min_severity")
            if minimum and (summary["max_severity"] is None or SEVERITIES.index(summary["max_severity"]) < SEVERITIES.index(minimum)):
                continue
            start = summary["started_at"] or ""
            if filters.get("since") and start < filters["since"] or filters.get("until") and start >= filters["until"]:
                continue
            result.append(summary)
        if len({item["session_id"] for item in result}) != len(result):
            raise QueryError(503, "store_unavailable")
        result.sort(key=lambda item: (item["started_at"] or "", item["session_id"]), reverse=True)
        return cursor_page(result, limit, cursor, scope="sessions", filters=filters,
                           key=lambda item: (item["started_at"] or "", item["session_id"]))

    def _list_filters(self, limit, filters, *, enums, exact, booleans=()):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise QueryError(400, "bad_request")
        safe = {}
        try:
            for key, value in filters.items():
                if value is None:
                    continue
                if key in enums:
                    values = value.split(",") if isinstance(value, str) else list(value)
                    if not values or any(item not in enums[key] for item in values):
                        raise ValueError
                    safe[key] = sorted(set(values))
                elif key in exact:
                    safe[key] = token(value, required=True)
                elif key in booleans:
                    if type(value) is not bool:
                        raise ValueError
                    safe[key] = value
                elif key in ("since", "until"):
                    safe[key] = timestamp(value)
                else:
                    raise ValueError
            if safe.get("since") and safe.get("until") and safe["since"] >= safe["until"]:
                raise ValueError
        except (ValueError, TypeError):
            raise QueryError(400, "invalid_filter") from None
        return safe

    def _check_expired(self, bundle):
        if bundle["run"] and bundle["run"]["lifecycle"] == "EXPIRED":
            raise QueryError(410, "evidence_expired")

    def actions(self, *, limit=100, cursor=None, include_intents=False, **filters):
        if type(include_intents) is not bool:
            raise QueryError(400, "invalid_filter")
        filters = self._list_filters(limit, filters, enums={
            "kinds": ("prompt", "tool_use", "egress", "session", "approval", "control"),
            "statuses": ("completed", "blocked", "redacted", "pending_approval", "failed", "pending"),
            "decisions": tuple(DECISION_FOR_VERDICT.values()),
            "side_effects": ("read", "write", "irreversible")},
            exact=("session_id", "run_id", "case_id", "agent_id", "action_id", "name"))
        result, keys, seen = [], {}, set()
        for path, bundle in self._iter_bundles(filters.get("session_id")):
            # Scope relevance survives pruning through the pinned contract. Do
            # not pretend an expired scope is an empty result for narrow filters.
            scope = {key: value for key, value in filters.items() if key in ("run_id", "case_id", "agent_id")}
            contract = bundle["contract"] or {}
            identities = [contract] + [{"run_id": event.context.run_id, "case_id": event.case_id,
                                       "agent_id": event.agent_id} for event in bundle["events"]]
            if not any(all(identity.get(key) == value for key, value in scope.items()) for identity in identities):
                continue
            self._check_expired(bundle)
            for event in bundle["events"]:
                if is_intent(event) and not include_intents:
                    continue
                row = self._action(event, bundle)["summary"]
                if any(row[key] != value for key, value in filters.items()
                       if key in ("session_id", "run_id", "case_id", "agent_id", "action_id", "name")):
                    continue
                if any(row[column] not in filters[key] for key, column in
                       (("kinds", "kind"), ("statuses", "status"), ("decisions", "decision"), ("side_effects", "side_effect"))
                       if key in filters):
                    continue
                if filters.get("since") and row["ts"] < filters["since"] or filters.get("until") and row["ts"] >= filters["until"]:
                    continue
                if row["event_id"] in seen:
                    raise QueryError(503, "store_unavailable")
                seen.add(row["event_id"])
                keys[row["event_id"]] = (row["ts"], path.name, bundle["_rowids"][event.event_id], event.event_id)
                result.append(row)
        result.sort(key=lambda row: keys[row["event_id"]])
        return cursor_page(result, limit, cursor, scope="actions", filters={**filters, "include_intents": include_intents},
                           key=lambda row: keys[row["event_id"]])

    def interventions(self, *, limit=100, cursor=None, **filters):
        filters = self._list_filters(limit, filters, enums={
            "actions": ("ALERT", "REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS", "STRICT_MODE", "HALT_SESSION")},
            exact=("session_id", "source_plugin"), booleans=("applied", "active"))
        result, keys, seen = [], {}, set()
        for path, bundle in self._iter_bundles(filters.get("session_id")):
            self._check_expired(bundle)
            for row in bundle["interventions"]:
                if any(row[key] != value for key, value in filters.items()
                       if key in ("session_id", "source_plugin", "applied", "active")):
                    continue
                if filters.get("actions") and row["action"] not in filters["actions"]:
                    continue
                if filters.get("since") and row["ts"] < filters["since"] or filters.get("until") and row["ts"] >= filters["until"]:
                    continue
                if row["signal_id"] in seen:
                    raise QueryError(503, "store_unavailable")
                seen.add(row["signal_id"])
                keys[row["signal_id"]] = (row["ts"], path.name, bundle["_signal_rowids"][row["signal_id"]], row["signal_id"])
                result.append(row)
        result.sort(key=lambda row: keys[row["signal_id"]])
        return cursor_page(result, limit, cursor, scope="interventions", filters=filters,
                           key=lambda row: keys[row["signal_id"]])

    def action(self, event_id):
        match = None
        budget = {"rows": 0, "bytes": 0}
        for path in self.paths():
            with self.connect(path, budget=budget) as conn:
                rows = self._rows(conn, "SELECT session_id FROM events WHERE event_id=?", (event_id,))
                row = rows[0] if rows else None
                if row:
                    if match is not None:
                        raise QueryError(503, "store_unavailable")
                    bundle = self._load_session(conn, row[0])
                    event = next(item for item in bundle["events"] if item.event_id == event_id)
                    match = self._action(event, bundle)
        if match is None:
            raise QueryError(404, "not_found")
        return match

    def trajectory(self, session_id, *, limit=1000, cursor=None, view="summary", include_detections=True, **filters):
        with self.connect(self._session_path(session_id)) as conn:
            bundle = self._load_session(conn, session_id)
            session = self._session(bundle)
            events = [item for item in bundle["events"] if not is_intent(item)]
            summaries = [self._action(event, bundle)["summary"] for event in events]
            selected = [row for row in summaries
                        if (not filters.get("kinds") or row["kind"] in filters["kinds"])
                        and (not filters.get("statuses") or row["status"] in filters["statuses"])
                        and (filters.get("from_seq") is None or row["seq"] >= filters["from_seq"])
                        and (filters.get("to_seq") is None or row["seq"] <= filters["to_seq"])
                        and (not filters.get("since") or row["ts"] >= filters["since"])
                        and (not filters.get("until") or row["ts"] < filters["until"])]
            paged = cursor_page(selected, limit, cursor, scope=f"session:{session_id}",
                                filters={**filters, "view": view, "include_detections": include_detections},
                                key=lambda row: (row["seq"], row["event_id"]))
            by_id = {event.event_id: event for event in events}
            steps = [self._action(by_id[row["event_id"]], bundle) if view == "full" else dict(row) for row in paged["items"]]
            if not include_detections:
                for step in steps:
                    (step["summary"] if view == "full" else step)["detections"] = []
                    if view == "full": step["detections"] = []
            gaps = []
            if not filters.get("kinds") and not filters.get("statuses"):
                seqs = [row["seq"] for row in selected]
                gaps = [{"after_seq": a, "before_seq": b} for a, b in zip(seqs, seqs[1:]) if b != a + 1]
            segment = {key: session[key] for key in ("session_id", "run_id", "agent_id", "case_id", "contract_id", "state", "started_at", "ended_at", "end_reason")}
            segment.update(steps=steps, gaps=gaps)
            totals = {"steps": len(selected), "executed": sum(row["executed"] for row in selected),
                      **{status: sum(row["status"] == status for row in selected) for status in ("blocked", "redacted", "pending_approval", "failed")},
                      "detections": session["detection_count"], "max_severity": session["max_severity"], "usage": session["usage"]}
            return {"scope": "session", "id": session_id, "ordering": "seq", "segments": [segment], "totals": totals,
                    "risk": {"level": session["risk_level"], "expected_loss": None, "failure_probability": None,
                             "finding_id": next((item["finding_id"] for item in bundle["findings"] if item.get("plugin") == "trajectory-risk"), None),
                             "source": "trajectory-risk"} if session["risk_level"] else None,
                    "verification_status": session["verification_status"], "next_cursor": paged["next_cursor"], "has_more": paged["has_more"]}

    # --- control-plane decision trace ---------------------------------------------------------

    def _decision_rows(self, conn, where, args):
        # Stores written before the decision trace existed have no table: that is "no decisions".
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plugin_decisions'").fetchone():
            return []
        rows = self._rows(conn, f"SELECT payload_json FROM plugin_decisions WHERE {where} ORDER BY ts, rowid", args)
        # Re-project on read: a manually edited database cannot leak prose or raw values.
        return [decision_projection(json.loads(row[0])) for row in rows]

    def _with_context(self, item, bundle):
        """Attach the trigger step and linked findings so one response explains the decision."""
        event = next((e for e in bundle["events"] if e.event_id == item["trigger_event_id"]), None)
        trigger = None
        if event is not None:
            summary = self._summary(event)
            trigger = {key: summary.get(key) for key in ("event_id", "seq", "ts", "kind", "name", "status", "decision")}
        findings = [f for f in bundle["findings"] if f["finding_id"] in set(item["finding_ids"])]
        return {**item, "trigger": trigger, "findings": findings}

    def decisions(self, session_id, *, limit=100, cursor=None, **filters):
        with self.connect(self._session_path(session_id)) as conn:
            bundle = self._load_session(conn, session_id, allow_expired=True)
            items = self._decision_rows(conn, "session_id=?", (session_id,))
        items = [item for item in items
                 if (not filters.get("plugins") or item["plugin"] in filters["plugins"])
                 and (not filters.get("outcomes") or item["outcome"] in filters["outcomes"])
                 and (not filters.get("decisions") or item["decision"] in filters["decisions"])
                 and (not filters.get("trigger_event_id") or item["trigger_event_id"] == filters["trigger_event_id"])]
        page = cursor_page(items, limit, cursor, scope=f"decisions:{session_id}", filters=filters,
                           key=lambda item: (item["ts"], item["decision_id"]))
        page["items"] = [self._with_context(item, bundle) for item in page["items"]]
        counts = Counter((item["plugin"], item["outcome"]) for item in items)
        page["summary"] = [{"plugin": plugin, "outcome": outcome, "count": count}
                           for (plugin, outcome), count in sorted(counts.items())]
        return page

    def decision(self, decision_id):
        if not ID.fullmatch(decision_id):
            raise QueryError(400, "bad_request")
        match = None
        for path in self.paths():
            with self.connect(path) as conn:
                rows = self._decision_rows(conn, "decision_id=?", (decision_id,))
                if rows:
                    if match is not None:
                        raise QueryError(503, "store_unavailable")
                    bundle = self._load_session(conn, rows[0]["session_id"], allow_expired=True)
                    match = self._with_context(rows[0], bundle)
        if match is None:
            raise QueryError(404, "not_found")
        return match

    def export(self, session_id):
        with self.connect(self._session_path(session_id)) as conn:
            bundle = self._load_session(conn, session_id)
            settings_json = self._rows(conn, "SELECT settings_json FROM store_metadata WHERE singleton=1")[0]
            settings = PersistenceSettings.from_policy(json.loads(settings_json[0]))
            records = [{"record_type": "session", **self._detail(bundle)}]
            for event in bundle["events"]:
                records.append({"record_type": "intent" if is_intent(event) else "action", **self._action(event, bundle)["event"]})
            records.extend({"record_type": "detection", **item} for item in self._detections(bundle))
            records.extend({"record_type": "intervention", **item} for item in bundle["interventions"])
            records.extend({"record_type": "plugin_decision", **item}
                           for item in self._decision_rows(conn, "session_id=?", (session_id,)))
            if bundle["verification"]:
                records.append({"record_type": "verification", **bundle["verification"]})
            if len(records) > settings.max_export_rows:
                raise QueryError(413, "export_quota_exceeded")
            lines = [json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records]
            payload = "".join(lines).encode()
            footer = {"record_type": "export_footer", "rows": len(records), "sha256": hashlib.sha256(payload).hexdigest(), "exported_at": now()}
            payload += (json.dumps(footer, sort_keys=True, separators=(",", ":")) + "\n").encode()
            if len(payload) > settings.max_export_bytes:
                raise QueryError(413, "export_quota_exceeded")
            return payload
