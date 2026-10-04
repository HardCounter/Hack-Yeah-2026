"""Persisted usage, not a projection of the gateway's live budget ledger.

Token usage is estimated resolved dispatch usage; token budget charges are the
conservative durable intent reservations (never refunded on failure/output deny).
Legacy results without intents are disclosed and make budget reconstruction
incomplete. Tool charges cannot be reconstructed exactly: both Policy and the
in-memory BudgetGuard can charge before a late veto. Their remaining/utilisation
are therefore always null. Financial pricing is unsupported, including stored
zero costs: null means unavailable, not free. Percentiles use nearest rank and
are null for empty samples. Buckets are UTC, anchored at since, with a final
partial bucket. Scanner bounds and corrupt/expired handling belong to ReadQueries.
Call totals include pending calls once. Blocked counts are final decisions, not
execution states: they can overlap failed (or committed tool) outcomes. Usage
completeness includes unresolved calls; budget completeness instead means all
conservative charges are reconstructable, even when a result is still pending.
"""
from collections import Counter
from datetime import timedelta
import math

from persistence.models import ActionStatus, ActionType, parse_utc_iso_timestamp
from persistence.privacy import token
from persistence.vocabulary import DECISION_FOR_VERDICT, is_intent

GATEWAY_TYPES = frozenset({ActionType.LLM_INVOCATION, ActionType.TOOL_CALL,
                           ActionType.MCP_TOOL, ActionType.EGRESS_HTTP})
ADMITTED = frozenset({"ALLOW", "ALERT", "REDACT"})
BUCKET_SECONDS = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}
IMPLEMENTED_METRICS = frozenset({"actions", "blocked", "redacted", "input_tokens", "output_tokens"})
DEFERRED_METRICS = frozenset({"detections", "cost_usd", "interception_overhead_ms_p95"})
BUDGET_REASONS = frozenset({"BUDGET_EXHAUSTED", "TOKEN_BUDGET_EXHAUSTED",
                            "TOOL_CALL_BUDGET_EXHAUSTED", "LOCAL_MODEL_COST_BUDGET_UNSUPPORTED",
                            "COST_BUDGET_UNSUPPORTED", "TOKEN_BUDGET_REQUIRED", "BUDGET_STATE_LIMIT",
                            "BUDGET_ACTION_CAPACITY", "BUDGET_RESERVATION_CONFLICT",
                            "INVALID_BUDGET_USAGE", "INVALID_BUDGET_REASON"})
MODEL_PRE_DISPATCH_REASONS = BUDGET_REASONS | frozenset({
    "UNKNOWN_SESSION", "SESSION_HALTED", "SESSION_FINISHED", "STRICT_MODE_LLM_DENIED",
    "DYNAMIC_LLM_RESTRICTION", "SESSION_ADMISSION_UNAVAILABLE", "SESSION_ADMISSION_DENIED",
    "MODEL_NOT_AUTHORIZED", "INVALID_PROMPT_ARGUMENTS", "APPROVAL_REQUIRED",
})


def _decision(event):
    return DECISION_FOR_VERDICT[event.interception_metadata.verdict]


def _logical_calls(events):
    """One call per session/action ID; ambiguous duplicates fail closed.

Missing legacy action IDs are not correlated by name/time: each event remains
independent. Cross-kind/name/agent identity changes cannot prove a dispatch.
"""
    groups = {}
    for event in events:
        if event.action_type not in GATEWAY_TYPES:
            continue
        key = (event.session_id, event.context.action_id or ("event", event.event_id))
        groups.setdefault(key, []).append(event)
    calls = []
    for group in groups.values():
        intents = [event for event in group if is_intent(event)]
        results = [event for event in group if not is_intent(event)]
        identities = {(event.action_type, event.agent_id, event.action_details.name,
                       event.action_details.wire_details.get("model"), event.context.run_id)
                      for event in group}
        if len(intents) > 1 or len(results) > 1 or len(identities) != 1:
            raise ValueError("ambiguous call evidence")
        calls.append((intents[0] if intents else None, results[0] if results else None))
    return calls


def model_usage_eligibility(events):
    """Pure shared eligibility/accounting helper, used before timeseries filtering.

Returns resolved chargeable events, unresolved intents and conservative budget
charges. A pending intent is reservation evidence, never completed usage. Reject
bounds on standalone BLOCK/approval records are not usage or reservations.
Missing usage stays unknown; legacy failed/BLOCK evidence without an intent is
excluded as ambiguous rather than promoted to a dispatch from nonzero estimates.
"""
    resolved, pending, legacy_events, uncertain_events = [], [], [], []
    used, reserved, complete, legacy, uncertain = 0, 0, True, 0, 0
    # Tool retry evidence is irrelevant to model accounting. Only correlate
    # model records here; ambiguous tool calls must not poison token charts.
    for intent, result in _logical_calls(
            event for event in events if event.action_type == ActionType.LLM_INVOCATION):
        event = result or intent
        if event.action_type != ActionType.LLM_INVOCATION:
            continue
        if intent:
            if _decision(intent) not in ADMITTED or not intent.context.action_id:
                raise ValueError("invalid model intent")
            bound = intent.context.reserved_usage.get("reserved_tokens")
            if result and (result.status not in (ActionStatus.EXECUTED, ActionStatus.REDACTED, ActionStatus.FAILED)
                           or result.context.reason_code in MODEL_PRE_DISPATCH_REASONS):
                raise ValueError("model intent contradicts rejection")
            if bound is None:
                complete = False
            elif result:
                used += bound
            else:
                reserved += bound
            if result:
                resolved.append(result)
            else:
                pending.append(intent)
        elif result.status in (ActionStatus.EXECUTED, ActionStatus.REDACTED, ActionStatus.FAILED):
            if _decision(result) in ADMITTED and result.context.reason_code not in MODEL_PRE_DISPATCH_REASONS:
                resolved.append(result)
                legacy += 1
                legacy_events.append(result)
                complete = False
                # This is only a disclosed reconstruction, not durable charge proof.
                bound = result.context.reserved_usage.get("reserved_tokens")
                actual = result.context.actual_usage
                if bound is not None:
                    used += bound
                elif "input_tokens" in actual and "output_tokens" in actual:
                    used += actual["input_tokens"] + actual["output_tokens"]
            else:
                uncertain += 1
                uncertain_events.append(result)
                complete = False
    return {"resolved": resolved, "pending": pending, "used": used, "reserved": reserved,
            "complete": complete, "legacy_calls": legacy, "uncertain_calls": uncertain,
            "legacy_events": legacy_events, "uncertain_events": uncertain_events}


def _sum_measurement(events, key):
    values = [event.context.actual_usage.get(key) for event in events]
    return None if any(value is None for value in values) else sum(values)


def _percentile(values, percentile):
    return sorted(values)[math.ceil(len(values) * percentile) - 1] if values else None


def _iso(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _budget(resource, limit, used, reserved, *, complete, source):
    known = complete and used is not None and reserved is not None
    charged = used + reserved if used is not None and reserved is not None else None
    return {"resource": resource, "limit": limit, "used": used, "reserved": reserved,
            "remaining": max(0, limit - charged) if known and limit is not None else None,
            "utilisation": charged / limit if known and limit not in (None, 0) else None,
            "exceeded": (charged > limit if charged is not None and limit is not None
                         and (known or charged > limit) else False if known else None),
            "scope": "session", "accounting_source": source, "complete": complete}


class UsageQueriesMixin:
    def session_usage(self, session_id):
        from persistence.query import QueryError

        with self.connect(self._session_path(session_id)) as conn:
            bundle = self._load_session(conn, session_id)
            try:
                return self._session_usage(bundle)
            except (ValueError, KeyError, TypeError):
                raise QueryError(503, "store_unavailable") from None

    def _session_usage(self, bundle):
        events = bundle["events"]
        model = model_usage_eligibility(events)
        calls = _logical_calls(events)
        rows = [self._summary(result or intent) for intent, result in calls]
        models = [row for row in rows if row["kind"] == "prompt"]
        tools = [row for row in rows if row["kind"] == "tool_use"]
        egress = [row for row in rows if row["kind"] == "egress"]
        resolved = model["resolved"]
        inputs = _sum_measurement(resolved, "input_tokens")
        outputs = _sum_measurement(resolved, "output_tokens")
        usage = {"input_tokens": inputs, "output_tokens": outputs,
                 "total_tokens": inputs + outputs if inputs is not None and outputs is not None else None,
                 "cost_usd": None, "latency_ms": _sum_measurement(resolved, "latency_ms"),
                 "source": "estimated", "cost_source": "unavailable",
                 "complete": not model["uncertain_calls"] and not model["pending"]
                 and inputs is not None and outputs is not None,
                 "legacy_calls": model["legacy_calls"], "uncertain_calls": model["uncertain_calls"],
                 "pending_calls": len(model["pending"])}
        # Dispatch evidence is separate from the final delivery decision. A tool
        # intent followed by BLOCK can be a late veto: it does not prove execution.
        admitted_tools = []
        tool_pending = 0
        for intent, result in calls:
            event = result or intent
            if event.action_type not in (ActionType.TOOL_CALL, ActionType.MCP_TOOL):
                continue
            if result is None:
                tool_pending += 1
            elif result.status in (ActionStatus.EXECUTED, ActionStatus.REDACTED, ActionStatus.FAILED) and (
                    _decision(result) in ADMITTED or result.context.reason_code in
                    ("OUTPUT_INSPECTION_BLOCK", "CALL_CANCELLED_AFTER_EXECUTION", "EFFECT_COMMITTED")):
                admitted_tools.append(result)
        backend_events = resolved + admitted_tools
        backend = [event.context.actual_usage["latency_ms"] for event in backend_events
                   if "latency_ms" in event.context.actual_usage]
        overhead = [row["interception_overhead_ms"] for row in rows
                    if row["status"] != "pending" and row["interception_overhead_ms"] is not None]
        times = sorted(parse_utc_iso_timestamp(event.ts) for event in events)
        limits = (bundle.get("contract") or {}).get("budget", {})
        tool_groups, by_tool = [], {}
        for row in tools:
            by_tool.setdefault(row["name"], []).append(row)
        for name, group in sorted(by_tool.items()):
            effects = {row["side_effect"] for row in group}
            tool_groups.append({"tool": name, "total": len(group),
                                "blocked": sum(row["decision"] == "BLOCK" for row in group),
                                "side_effect": next(iter(effects)) if len(effects) == 1 else None})
        return {"session_id": bundle["session_id"],
                "window": {"first_ts": _iso(times[0]) if times else None,
                           "last_ts": _iso(times[-1]) if times else None,
                           "duration_s": (times[-1] - times[0]).total_seconds() if times else None},
                "usage": usage,
                "model_calls": {"total": len(models),
                                "completed": sum(row["status"] in ("completed", "redacted") for row in models),
                                "blocked": sum(row["decision"] == "BLOCK" for row in models),
                                "failed": sum(row["status"] == "failed" for row in models),
                                "pending": len(model["pending"]),
                                "pending_approval": sum(row["status"] == "pending_approval" for row in models),
                                "dispatched": len(resolved), "incomplete": len(model["pending"]) + model["uncertain_calls"],
                                "by_model": dict(Counter(row["name"] for row in models))},
                "tool_calls": {"total": len(tools), "executed": sum(row["executed"] for row in tools),
                               "blocked": sum(row["decision"] == "BLOCK" for row in tools),
                               "pending_approval": sum(row["status"] == "pending_approval" for row in tools),
                               "failed": sum(row["status"] == "failed" for row in tools),
                               "pending": tool_pending, "dispatched": len(admitted_tools), "by_tool": tool_groups},
                "egress_calls": {"total": len(egress), "blocked": sum(row["decision"] == "BLOCK" for row in egress),
                                 "pending": sum(row["status"] == "pending" for row in egress),
                                 "by_host": dict(Counter(row["name"] for row in egress))},
                "latency_ms": {"backend_sum": sum(backend), "action_p50": _percentile(backend, .5),
                               "action_p95": _percentile(backend, .95),
                               "interception_overhead_p50": _percentile(overhead, .5),
                               "interception_overhead_p95": _percentile(overhead, .95),
                               "percentile_method": "nearest_rank", "backend_samples": len(backend),
                               "complete": len(backend) == len(backend_events)},
                 "budgets": [_budget("tokens", limits.get("tokens"), model["used"], model["reserved"],
                                     complete=model["complete"] and "tokens" in limits,
                                     source="mixed_reconstruction" if model["legacy_calls"]
                                    else "durable_intent_reservations"),
                            _budget("tool_calls", limits.get("tool_calls"), len(admitted_tools), tool_pending,
                                    complete=False, source="reconstructable_admitted_dispatches"),
                            _budget("cost_usd", limits.get("cost_usd"), None, None,
                                    complete=False, source="unavailable")],
                "budget_blocks": sum(row["decision"] == "BLOCK" and (
                    row["reason_code"] in BUDGET_REASONS or any(rule["rule_id"] in BUDGET_REASONS
                                                              for rule in row["triggered_rules"])) for row in rows)}

    def timeseries(self, *, metric, bucket, since, until, session_id=None, agent_id=None):
        from persistence.query import QueryError

        if metric in DEFERRED_METRICS:
            raise QueryError(501, "not_implemented")
        if metric not in IMPLEMENTED_METRICS or bucket not in BUCKET_SECONDS:
            raise QueryError(400, "bad_request")
        try:
            lo, hi = parse_utc_iso_timestamp(since), parse_utc_iso_timestamp(until)
            for value in (session_id, agent_id):
                if value is not None:
                    token(value, required=True)
            width = timedelta(seconds=BUCKET_SECONDS[bucket])
            duration = hi - lo
            count = duration // width + bool(duration % width)
            if hi <= lo or count > 1000:
                raise ValueError
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise QueryError(400, "bad_request") from None
        points = [{"ts": _iso(lo + index * width), "value": 0} for index in range(count)]
        legacy, uncertain, pending = 0, 0, 0
        for _, bundle in self._iter_bundles(session_id=session_id):
            # Expired evidence may have lost every event timestamp to pruning.
            # Match scope using the pinned contract as well as surviving events,
            # then reject expiration before any time filtering. An unrelated
            # agent's expired store must not poison this agent's series.
            if agent_id is not None and not (
                    (bundle.get("contract") or {}).get("agent_id") == agent_id
                    or any(event.agent_id == agent_id for event in bundle["events"])):
                continue
            self._check_expired(bundle)
            try:
                if metric in ("input_tokens", "output_tokens"):
                    eligible = model_usage_eligibility(bundle["events"])
                    selected = eligible["resolved"]
                    legacy += sum((agent_id is None or event.agent_id == agent_id)
                                  and lo <= parse_utc_iso_timestamp(event.ts) < hi
                                  for event in eligible["legacy_events"])
                    uncertain += sum((agent_id is None or event.agent_id == agent_id)
                                     and lo <= parse_utc_iso_timestamp(event.ts) < hi
                                     for event in eligible["uncertain_events"])
                    pending += sum((agent_id is None or event.agent_id == agent_id)
                                   and lo <= parse_utc_iso_timestamp(event.ts) < hi
                                   for event in eligible["pending"])
                else:
                    # Count observable interactions, not reconstructed dispatches.
                    # Rejected retries can reuse an action ID without becoming
                    # another dispatch; each final decision remains an event.
                    selected = [event for event in bundle["events"]
                                if event.action_type in GATEWAY_TYPES and not is_intent(event)]
                for event in selected:
                    if agent_id is not None and event.agent_id != agent_id:
                        continue
                    ts = parse_utc_iso_timestamp(event.ts)
                    if not lo <= ts < hi:
                        continue
                    value = (event.context.actual_usage.get(metric) if metric in ("input_tokens", "output_tokens")
                             else int(metric == "actions" or _decision(event) == {"blocked": "BLOCK", "redacted": "REDACT"}[metric]))
                    # BLOCKED -> BLOCK, REDACTED -> REDACT (not status-based).
                    index = (ts - lo) // width
                    current = points[index]["value"]
                    points[index]["value"] = current + value if current is not None and value is not None else None
            except (ValueError, TypeError, KeyError):
                raise QueryError(503, "store_unavailable") from None
        result = {"metric": metric, "bucket": bucket, "since": _iso(lo), "until": _iso(hi), "points": points}
        if metric in ("input_tokens", "output_tokens"):
            result.update(source="estimated", complete=not uncertain and not pending
                          and all(point["value"] is not None for point in points),
                          legacy_calls=legacy, uncertain_calls=uncertain, pending_calls=pending)
        return result
