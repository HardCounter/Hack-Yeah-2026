"""Dashboard aggregates from persisted evidence, with bounded scans and no sample values."""
from collections import Counter, defaultdict
import hashlib
import json
import tempfile

from persistence.models import ActionType
from persistence.query_usage import _percentile, model_usage_eligibility
from persistence.vocabulary import DECISION_FOR_VERDICT, is_intent

# Only registered gateway controls have known methods. Unknown historic/custom
# auditors keep an unknown method rather than being assumed deterministic.
METHODS = {"policy": "deterministic", "pattern-match": "deterministic",
           "velocity-guard": "deterministic", "budget-guard": "deterministic",
           "signature-scanner": "deterministic", "privacy-scanner": "deterministic",
           "secret-scanner": "deterministic", "domain-blocklist": "deterministic",
           "tool-allowlist": "deterministic"}
FAMILIES = {"pattern-match": "injection", "budget-guard": "budget",
            "velocity-guard": "budget", "trajectory-risk": "trajectory",
            "outcome-verifier": "outcome", "goal-alignment-judge": "outcome",
            "signature-scanner": "exploit_signature", "privacy-scanner": "pii", "secret-scanner": "secrets"}


def quantiles(values):
    values = [value for value in values if value is not None]
    return {"p50": _percentile(values, .5), "p95": _percentile(values, .95),
            "p99": _percentile(values, .99), "max": max(values) if values else None}


def within(ts, since=None, until=None):
    return ts is not None and (since is None or ts >= since) and (until is None or ts < until)


def usage_total(events):
    measured = [event.context.actual_usage for event in events]
    totals = {key: sum(item[key] for item in measured) if all(key in item for item in measured) else None
              for key in ("input_tokens", "output_tokens", "latency_ms")}
    totals["total_tokens"] = (totals["input_tokens"] + totals["output_tokens"]
                              if totals["input_tokens"] is not None and totals["output_tokens"] is not None else None)
    return {**totals, "cost_usd": None, "cost_source": "unavailable", "source": "estimated"}


class DashboardQueriesMixin:
    def _dashboard_bundles(self, *, session_id=None, agent_id=None, case_id=None):
        for _, bundle in self._iter_bundles(session_id):
            identities = [bundle["contract"] or {}] + [
                {"agent_id": event.agent_id, "case_id": event.case_id} for event in bundle["events"]]
            if not any((agent_id is None or item.get("agent_id") == agent_id)
                       and (case_id is None or item.get("case_id") == case_id) for item in identities):
                continue
            self._check_expired(bundle)
            yield bundle

    def _selected_events(self, bundle, since, until, agent_id=None, case_id=None):
        return [event for event in bundle["events"] if not is_intent(event)
                and within(event.ts, since, until)
                and (agent_id is None or event.agent_id == agent_id)
                and (case_id is None or event.case_id == case_id)]

    def detections(self, *, limit=100, cursor=None, order="desc", **filters):
        from persistence.query import QueryError, SEVERITIES, cursor_page
        names = filters.pop("names", None)
        minimum = filters.pop("min_severity", None)
        if order not in ("asc", "desc") or (minimum is not None and minimum not in SEVERITIES):
            raise QueryError(400, "invalid_filter")
        filters = self._list_filters(limit, filters, enums={"sources": ("gateway", "finding", "alert", "verification")},
                                     exact=("session_id", "run_id", "case_id", "agent_id", "trigger_event_id"))
        names = names.split(",") if isinstance(names, str) else names
        result, seen = [], set()
        for bundle in self._dashboard_bundles(session_id=filters.get("session_id"), agent_id=filters.get("agent_id"),
                                              case_id=filters.get("case_id")):
            for item in self._detections(bundle):
                if any(item.get(key) != value for key, value in filters.items()
                       if key in ("session_id", "run_id", "case_id", "agent_id", "trigger_event_id")):
                    continue
                if filters.get("sources") and item["source"] not in filters["sources"]:
                    continue
                if names is not None and item["name"] not in names:
                    continue
                if minimum is not None and SEVERITIES.index(item["severity"]) < SEVERITIES.index(minimum):
                    continue
                if (filters.get("since") or filters.get("until")) and not within(item["ts"], filters.get("since"), filters.get("until")):
                    continue
                if item["detection_id"] in seen:
                    raise QueryError(503, "store_unavailable")
                seen.add(item["detection_id"])
                result.append(item)
        result.sort(key=lambda row: (row["ts"] or "", row["detection_id"]), reverse=order == "desc")
        return cursor_page(result, limit, cursor, scope="detections",
                           filters={**filters, "names": names, "min_severity": minimum, "order": order},
                           key=lambda row: (row["ts"] or "", row["detection_id"]))

    def detection(self, detection_id):
        from persistence.query import QueryError
        match = None
        for bundle in self._dashboard_bundles():
            for item in self._detections(bundle):
                if item["detection_id"] == detection_id:
                    if match is not None:
                        raise QueryError(503, "store_unavailable")
                    match = item
        if match is None:
            raise QueryError(404, "not_found")
        return match

    def detection_catalog(self):
        from persistence.query import SEVERITIES
        grouped = defaultdict(list)
        for bundle in self._dashboard_bundles():
            for item in self._detections(bundle):
                grouped[item["name"]].append(item)
        return {"catalog_version": "persisted-v1", "entries": [
            {"name": name, "title": name, "description": None,
             "default_severity": max((item["severity"] for item in rows), key=SEVERITIES.index),
             "reasons": {item["reason"]: item["reason"] for item in rows if item["reason"]},
             "owasp": [], "control_family": FAMILIES.get(rows[0]["detector"], "other")}
            for name, rows in sorted(grouped.items())]}

    def usage_report(self, *, group_by, since, until, limit=100, agent_id=None, case_id=None):
        groups, eligible = defaultdict(list), []
        selected, incomplete = [], False
        for bundle in self._dashboard_bundles(agent_id=agent_id, case_id=case_id):
            events = self._selected_events(bundle, since, until, agent_id, case_id)
            selected.extend(events)
            try:
                accounting = model_usage_eligibility(bundle["events"])
            except (ValueError, KeyError, TypeError):
                from persistence.query import QueryError
                raise QueryError(503, "store_unavailable") from None
            ids = {event.event_id for event in events}
            resolved = [event for event in accounting["resolved"] if event.event_id in ids]
            eligible.extend(resolved)
            incomplete |= any(within(event.ts, since, until) and (agent_id is None or event.agent_id == agent_id)
                              and (case_id is None or event.case_id == case_id)
                              for event in accounting["pending"] + accounting["uncertain_events"])
        for event in selected:
            row = self._summary(event)
            key = {"agent": row["agent_id"], "session": row["session_id"], "case": row["case_id"],
                   "model": row["name"] if row["kind"] == "prompt" else None,
                   "tool": row["name"] if row["kind"] == "tool_use" else None, "day": row["ts"][:10]}[group_by]
            if key is not None:
                groups[key].append(event)
        eligible_ids = {event.event_id for event in eligible}
        buckets = []
        for key, events in sorted(groups.items()):
            rows = [self._summary(event) for event in events]
            buckets.append({"key": key, "sessions": len({event.session_id for event in events}),
                            "model_calls": sum(row["kind"] == "prompt" for row in rows),
                            "tool_calls": sum(row["kind"] == "tool_use" for row in rows),
                            "blocked": sum(row["decision"] == "BLOCK" for row in rows),
                            "usage": usage_total([e for e in events if e.event_id in eligible_ids]), "budget": None})
        totals = usage_total(eligible)
        totals["complete"] = not incomplete and totals["total_tokens"] is not None
        return {"group_by": group_by, "since": since, "until": until, "totals": totals,
                "buckets": buckets[:limit], "has_more": len(buckets) > limit, "total_buckets": len(buckets)}

    def security_overview(self, *, since, until, top=10, session_id=None, agent_id=None):
        from persistence.query import SEVERITIES
        rows, detections, sessions, signals, policies, feeds = [], [], [], [], set(), set()
        for bundle in self._dashboard_bundles(session_id=session_id, agent_id=agent_id):
            events = self._selected_events(bundle, since, until, agent_id)
            found = [d for d in self._detections(bundle) if within(d["ts"], since, until)
                     and (agent_id is None or d["agent_id"] == agent_id)]
            interventions = [i for i in bundle["interventions"] if within(i["ts"], since, until)]
            summary = self._session(bundle)
            if events or found or interventions or within(summary["started_at"], since, until):
                sessions.append(summary)
            rows.extend(self._summary(event) for event in events)
            detections.extend(found)
            signals.extend(interventions)
            contract = bundle["contract"] or {}
            if events:
                policies.update(e.interception_metadata.policy_version for e in events if e.interception_metadata.policy_version)
                if contract.get("feed_version"):
                    feeds.add(contract["feed_version"])
        evaluated = [row for row in rows if row["decision"] is not None]
        decisions, kinds = Counter(row["decision"] for row in evaluated), Counter(row["kind"] for row in rows)
        states = Counter(s["state"] for s in sessions)
        verified = Counter(s["verification_status"] or "none" for s in sessions)
        names = Counter(d["name"] for d in detections)
        families = Counter(FAMILIES.get(d["detector"], "other") for d in detections)
        return {"since": since, "until": until,
                "actions": {"total": len(rows), "by_decision": {d: decisions[d] for d in ("ALLOW", "BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT")},
                            "by_kind": {k: kinds[k] for k in ("prompt", "tool_use", "egress", "session", "approval", "control")}},
                "block_rate": decisions["BLOCK"] / len(evaluated) if evaluated else 0,
                "redact_rate": decisions["REDACT"] / len(evaluated) if evaluated else 0,
                "detections": {"total": len(detections), "by_severity": dict(Counter(d["severity"] for d in detections)),
                               "by_source": dict(Counter(d["source"] for d in detections)), "by_control_family": dict(families)},
                "top_detections": [{"name": name, "count": count,
                                    "max_severity": max((d["severity"] for d in detections if d["name"] == name), key=SEVERITIES.index)}
                                   for name, count in names.most_common(top)],
                "top_blocked_tools": [{"tool": name, "count": count} for name, count in Counter(
                    r["name"] for r in rows if r["kind"] == "tool_use" and r["decision"] == "BLOCK").most_common(top)],
                "sessions": {"total": len(sessions), "active": states["active"], "halted": states["halted"],
                             "by_risk_level": dict(Counter(s["risk_level"] for s in sessions if s["risk_level"]))},
                "verification": {k: verified[k] for k in ("VERIFIED_SUCCESS", "FAILED_POSTCONDITIONS", "VERIFICATION_INCOMPLETE", "none")},
                "interventions": {"proposed": len(signals), "applied": sum(i["applied"] for i in signals), "active": sum(i["active"] for i in signals)},
                "interception_overhead_ms": quantiles([r["interception_overhead_ms"] for r in evaluated]),
                "policy_versions": sorted(policies), "feed_versions": sorted(feeds)}

    def performance_overview(self, *, since, until, session_id=None, agent_id=None):
        evaluated, backend, controls, methods = [], [], defaultdict(list), defaultdict(list)
        backend_expected = 0
        for bundle in self._dashboard_bundles(session_id=session_id, agent_id=agent_id):
            for event in self._selected_events(bundle, since, until, agent_id):
                row = self._summary(event)
                if row["decision"] is None:
                    continue
                evaluated.append(row)
                backend_expected += row["executed"]
                if row["executed"] and "latency_ms" in event.context.actual_usage:
                    backend.append(event.context.actual_usage["latency_ms"])
                by_method = defaultdict(list)
                for auditor in event.interception_metadata.auditor_decisions:
                    method = METHODS.get(auditor.auditor_name, "unknown")
                    controls[(auditor.auditor_name, method, "gateway")].append((auditor.latency_ms, DECISION_FOR_VERDICT[auditor.verdict] != "ALLOW", False))
                    by_method[method].append(auditor.latency_ms)
                for method, samples in by_method.items():
                    methods[method].append(sum(samples))
            for decision in bundle["decisions"]:
                if not within(decision["ts"], since, until) or (agent_id is not None and decision["agent_id"] != agent_id):
                    continue
                # A NO_CHANGE/SKIPPED/verified-success decision did not intervene.
                acted = bool(decision["finding_ids"] or decision["adjustments"])
                controls[(decision["plugin"], decision["method"], "control_plane")].append(
                    (decision["duration_ms"], acted, decision["outcome"] != "decided"))
        overhead = [row["interception_overhead_ms"] for row in evaluated if row["interception_overhead_ms"] is not None]
        return {"since": since, "until": until, "actions_evaluated": len(evaluated),
                "backend_actions_evaluated": backend_expected,
                "interception_overhead_ms": quantiles(overhead), "backend_latency_ms": quantiles(backend),
                "overhead_share": sum(overhead) / (sum(overhead) + sum(backend))
                if len(overhead) == len(evaluated) and len(backend) == backend_expected
                and backend and sum(overhead) + sum(backend) else None,
                "backend_latency_complete": len(backend) == backend_expected,
                "by_method": {method: {"runs": len(methods[method]), "skipped": None, **quantiles(methods[method])}
                              for method in ("deterministic", "semantic", "unknown")},
                "by_auditor": [{"auditor": name, "method": method, "plane": plane, "runs": len(samples),
                                "acted": sum(s[1] for s in samples), "failed": sum(s[2] for s in samples),
                                **quantiles([s[0] for s in samples])} for (name, method, plane), samples in sorted(controls.items())]}

    def export_actions(self, *, kinds=None, decisions=None, since=None, until=None, session_id=None, agent_id=None):
        """Spool a bounded export before headers, then stream it without a browser-sized buffer.

        The spool spills to disk at 1 MiB. Every store is read in its own transaction;
        the download is a per-store snapshot, with explicit quotas and integrity footer.
        """
        from persistence.query import QueryError, MAX_ROWS, MAX_READ_BYTES, now
        from persistence.settings import PersistenceSettings
        filters = self._list_filters(1000, dict(kinds=kinds, decisions=decisions, since=since, until=until,
                                              session_id=session_id, agent_id=agent_id),
                                     enums={"kinds": ("prompt", "tool_use", "egress", "session", "approval", "control"),
                                            "decisions": tuple(DECISION_FOR_VERDICT.values())}, exact=("session_id", "agent_id"))
        output = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        digest, count, size = hashlib.sha256(), 0, 0
        try:
            # First validate/read all evidence through the same bounded query path.
            # Quotas are enforced before returning the file to StreamingResponse.
            per_store = {}
            for path, bundle in self._iter_bundles(filters.get("session_id")):
                if filters.get("agent_id") and not (
                        (bundle["contract"] or {}).get("agent_id") == filters["agent_id"]
                        or any(event.agent_id == filters["agent_id"] for event in bundle["events"])):
                    continue
                self._check_expired(bundle)
                if path not in per_store:
                    with self.connect(path) as conn:
                        settings = PersistenceSettings.from_policy(json.loads(self._rows(conn, "SELECT settings_json FROM store_metadata WHERE singleton=1")[0][0]))
                    per_store[path] = [settings, 0, 0]
                state = per_store[path]
                for event in bundle["events"]:
                    if is_intent(event):
                        continue
                    row = self._summary(event)
                    if filters.get("agent_id") and row["agent_id"] != filters["agent_id"]:
                        continue
                    if filters.get("kinds") and row["kind"] not in filters["kinds"]:
                        continue
                    if filters.get("decisions") and row["decision"] not in filters["decisions"]:
                        continue
                    if not within(row["ts"], filters.get("since"), filters.get("until")):
                        continue
                    line = (json.dumps({"record_type": "action", **self._action(event, bundle)["event"]}, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    count += 1; size += len(line); state[1] += 1; state[2] += len(line)
                    if count > MAX_ROWS or size > MAX_READ_BYTES or state[1] > state[0].max_export_rows or state[2] > state[0].max_export_bytes:
                        raise QueryError(413, "export_quota_exceeded")
                    digest.update(line)
                    output.write(line)
            footer = {"record_type": "export_footer", "rows": count, "sha256": digest.hexdigest(), "exported_at": now()}
            output.write((json.dumps(footer, sort_keys=True, separators=(",", ":")) + "\n").encode())
            output.seek(0)
            return output
        except BaseException:
            output.close()
            raise
