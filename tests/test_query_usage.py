"""Usage read-side accounting with synthetic durable SQLite evidence only."""
import asyncio
import sqlite3

import pytest

from contracts import Budget, TaskContract
from persistence.events import build_action_event
from persistence.governed import GovernedPersistence
from persistence.query import QueryError, ReadQueries
from persistence.query_usage import model_usage_eligibility
from persistence.store import EventStore


Queries = ReadQueries


def contract(session="ses_usage", agent="agent_demo"):
    return TaskContract(contract_id=f"contract_{session}", run_id=f"run_{session}", session_id=session,
                        principal_id="principal_demo", agent_id=agent, case_id="APP-0001", role="analyst",
                        objective="private objective", target_ids=frozenset({"APP-0001"}),
                        allowed_tools=frozenset({"read_application"}), postconditions=("ONB-P1",),
                        budget=Budget(tokens=1000, tool_calls=20, cost_usd=0),
                        policy_version="policy_demo", policy_hash="a" * 64, feed_version="feed_demo")


def event(c, action, *, kind="llm_call", status="EXECUTED", decision=None,
          intent=False, reserved=100, inputs=10, outputs=20, latency=5, reason=None, second=0):
    usage = {key: value for key, value in (("input_tokens", inputs), ("output_tokens", outputs),
                                          ("latency_ms", latency), ("actual_cost", 0)) if value is not None}
    wire = {"llm_call": {"model": "model_demo"}, "session": {"phase": "started"},
            "control": {"change": "adjustment_applied"},
            "approval": {"decision": "approved", "target_event_id": "evt_demo"},
            "egress_http": {"method": "GET", "host": "demo.example"}}.get(kind, {})
    e = build_action_event(contract=c, action_type=kind, action_id=action, name="model_demo" if kind == "llm_call" else "read_application",
                           status=status, decision=decision, intent=intent, latency_ms=second + 1,
                           reserved_usage={} if reserved is None else {"reserved_tokens": reserved},
                           actual_usage=usage, reason_code=reason,
                           wire_details=wire)
    e.ts = f"2026-10-04T10:{second // 60:02}:{second % 60:02}Z"
    return e


def seed(directory, c, events):
    directory.mkdir(exist_ok=True)
    path = directory / f"{c.session_id}.evidence.db"
    async def write():
        store = EventStore(path)
        await store.initialize()
        persistence = GovernedPersistence(store)
        await persistence.persist_contract(c)
        for e in events:
            if e.status.value == "PENDING":
                await persistence.intent(e)
            else:
                await persistence.append(e)
        await store.close()
    asyncio.run(write())
    return path


def series(q, metric="input_tokens", **kwargs):
    return q.timeseries(metric=metric, bucket="1m", since="2026-10-04T10:00:00Z",
                        until="2026-10-04T10:02:01Z", **kwargs)


def test_dispatch_reservation_is_not_estimated_usage_and_reject_bounds_do_not_charge(tmp_path):
    c = contract()
    events = [event(c, "ok", intent=True, reserved=100), event(c, "ok", reserved=999, inputs=7, outputs=8),
              event(c, "pending", intent=True, reserved=200, second=1),
              event(c, "reject", status="BLOCK", reserved=800, inputs=400, outputs=400,
                    reason="TOKEN_BUDGET_EXHAUSTED", second=2)]
    seed(tmp_path, c, events)
    q = Queries(tmp_path)
    result = q.session_usage(c.session_id)
    assert result["usage"]["total_tokens"] == 15
    budget = result["budgets"][0]
    assert (budget["used"], budget["reserved"], budget["remaining"], budget["utilisation"]) == (100, 200, 700, .3)
    assert budget["complete"] is True
    assert result["model_calls"]["total"] == 3
    assert result["model_calls"]["pending"] == 1
    assert result["model_calls"]["completed"] == 1
    assert result["budget_blocks"] == 1
    assert result["usage"]["pending_calls"] == 1 and result["usage"]["complete"] is False
    assert [p["value"] for p in series(q)["points"]] == [7, 0, 0]
    assert series(q)["pending_calls"] == 1 and series(q)["complete"] is False
    assert [p["value"] for p in series(q, "actions")["points"]] == [2, 0, 0]


@pytest.mark.parametrize("reason,decision", [("MODEL_BACKEND_FAILED", "ALLOW"),
                                           ("MODEL_CALL_CANCELLED", "ALLOW"),
                                           ("OUTPUT_INSPECTION_BLOCK", "BLOCK")])
def test_dispatched_failed_cancelled_and_output_blocked_model_usage_retains_charge(tmp_path, reason, decision):
    c = contract()
    seed(tmp_path, c, [event(c, "call", intent=True),
                       event(c, "call", status="FAILED", decision=decision, reason=reason)])
    q = Queries(tmp_path)
    result = q.session_usage(c.session_id)
    assert result["usage"]["total_tokens"] == 30
    assert result["budgets"][0]["used"] == 100
    assert result["model_calls"]["total"] == result["model_calls"]["failed"] == 1
    assert series(q)["points"][0]["value"] == 10
    assert series(q, "blocked")["points"][0]["value"] == int(decision == "BLOCK")


def test_legacy_eligibility_disclosed_and_blocked_failed_without_intent_is_uncertain(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "executed"), event(c, "redacted", status="REDACT"),
                       event(c, "failed", status="FAILED"),
                       event(c, "ambiguous", status="FAILED", decision="BLOCK")])
    q = Queries(tmp_path)
    result = q.session_usage(c.session_id)
    assert result["usage"]["total_tokens"] == 90
    assert result["usage"]["legacy_calls"] == 3
    assert result["usage"]["uncertain_calls"] == 1
    assert result["usage"]["complete"] is False
    assert result["budgets"][0]["remaining"] is None
    assert series(q)["complete"] is False
    assert series(q)["points"][0]["value"] == 30


def test_tool_late_veto_and_output_failure_do_not_claim_live_remaining(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "veto", kind="tool_call", intent=True),
                       event(c, "veto", kind="tool_call", status="BLOCK", reason="SCOPE_CHANGED"),
                       event(c, "output", kind="tool_call", status="FAILED", decision="BLOCK",
                             reason="OUTPUT_INSPECTION_BLOCK"),
                       event(c, "ok", kind="tool_call"), event(c, "pending", kind="tool_call", intent=True)])
    result = Queries(tmp_path).session_usage(c.session_id)
    assert result["tool_calls"]["total"] == 4
    assert result["tool_calls"]["blocked"] == 2
    assert result["tool_calls"]["dispatched"] == 2
    assert result["tool_calls"]["pending"] == 1
    budget = result["budgets"][1]
    assert budget["used"] == 2 and budget["reserved"] == 1
    assert budget["complete"] is False and budget["remaining"] is None and budget["utilisation"] is None
    assert result["tool_calls"]["by_tool"][0]["total"] == 4


def test_unavailable_cost_is_not_zero_or_free_and_empty_percentiles_are_null(tmp_path):
    c = contract()
    seed(tmp_path, c, [])
    result = Queries(tmp_path).session_usage(c.session_id)
    assert result["usage"]["cost_usd"] is None and result["usage"]["cost_source"] == "unavailable"
    assert result["budgets"][2]["used"] is None and result["budgets"][2]["exceeded"] is None
    assert result["window"]["first_ts"] is None and result["window"]["duration_s"] is None
    assert result["latency_ms"]["action_p50"] is None


def test_nearest_rank_backend_and_overhead_percentiles_are_distinct(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, f"call{i}", latency=i * 10, second=i) for i in range(1, 5)] +
                      [event(c, "reject", status="BLOCK", second=10, latency=999)])
    latency = Queries(tmp_path).session_usage(c.session_id)["latency_ms"]
    assert latency["backend_sum"] == 100
    assert latency["action_p50"] == 20 and latency["action_p95"] == 40
    assert latency["interception_overhead_p50"] == 4
    assert latency["interception_overhead_p95"] == 11


def test_missing_usage_measurement_does_not_fabricate_zero(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "call", intent=True), event(c, "call", inputs=None)])
    q = Queries(tmp_path)
    result = q.session_usage(c.session_id)
    assert result["usage"]["input_tokens"] is None and result["usage"]["total_tokens"] is None
    assert series(q)["points"][0]["value"] is None
    assert series(q, "output_tokens")["points"][0]["value"] == 20


def test_utc_anchor_partial_bucket_inclusive_exclusive_and_filters(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "early", second=0), event(c, "first", second=1),
                       event(c, "boundary", second=61), event(c, "excluded", second=62)])
    other = contract("ses_other", "agent_other")
    seed(tmp_path, other, [event(other, "different", second=1)])
    q = Queries(tmp_path)
    result = q.timeseries(metric="actions", bucket="1m", since="2026-10-04T12:00:01+02:00",
                          until="2026-10-04T10:01:02Z", agent_id=c.agent_id)
    assert [p["value"] for p in result["points"]] == [1, 1]
    assert result["points"][0]["ts"] == "2026-10-04T10:00:01.000000Z"
    assert sum(p["value"] for p in series(q, "actions", session_id=c.session_id)["points"]) == 4


@pytest.mark.parametrize("metric", ["detections", "cost_usd", "interception_overhead_ms_p95"])
def test_deferred_metrics_are_501_even_on_empty_store(tmp_path, metric):
    with pytest.raises(QueryError) as error:
        series(Queries(tmp_path), metric)
    assert (error.value.status, error.value.code) == (501, "not_implemented")


@pytest.mark.parametrize("changes", [{"metric": "unknown"}, {"bucket": "1s"},
                                     {"since": "2026-10-04T10:00:00"},
                                     {"until": "2026-10-04T10:00:00Z"},
                                     {"until": "2026-10-05T02:40:00.000001Z"},
                                     {"agent_id": "invalid/id"}])
def test_invalid_timeseries_parameters_and_ceil_point_limit(tmp_path, changes):
    kwargs = dict(metric="actions", bucket="1m", since="2026-10-04T10:00:00Z", until="2026-10-04T11:00:00Z")
    kwargs.update(changes)
    with pytest.raises(QueryError) as error:
        Queries(tmp_path).timeseries(**kwargs)
    assert error.value.status == 400


def test_exactly_1000_points_permitted(tmp_path):
    result = Queries(tmp_path).timeseries(metric="actions", bucket="1m", since="2026-10-04T10:00:00Z",
                                          until="2026-10-05T02:40:00Z")
    assert len(result["points"]) == 1000


def test_contradictory_intent_result_and_duplicate_results_fail_closed(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "call", intent=True), event(c, "call", status="BLOCK")])
    with pytest.raises(QueryError) as error:
        Queries(tmp_path).session_usage(c.session_id)
    assert error.value.status == 503
    with pytest.raises(QueryError):
        series(Queries(tmp_path))
    with pytest.raises(ValueError):
        model_usage_eligibility([event(c, "duplicate"), event(c, "duplicate")])


def test_failed_record_with_pre_dispatch_reason_never_charges_nonzero_estimates(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "reject", status="FAILED", reason="TOKEN_BUDGET_EXHAUSTED")])
    result = Queries(tmp_path).session_usage(c.session_id)
    assert result["usage"]["total_tokens"] == 0
    assert result["budgets"][0]["used"] == 0
    assert result["usage"]["complete"] is False
    with pytest.raises(ValueError):
        model_usage_eligibility([event(c, "call", intent=True),
                                 event(c, "call", status="FAILED", reason="MODEL_NOT_AUTHORIZED")])


def test_lifecycle_control_and_approval_do_not_count_as_gateway_actions(tmp_path):
    c = contract()
    events = [event(c, "session", kind="session"), event(c, "control", kind="control", status="BLOCK"),
              event(c, "approval", kind="approval"), event(c, "model", status="REDACT")]
    seed(tmp_path, c, events)
    q = Queries(tmp_path)
    assert series(q, "actions")["points"][0]["value"] == 1
    assert series(q, "blocked")["points"][0]["value"] == 0
    assert series(q, "redacted")["points"][0]["value"] == 1


def test_intent_before_window_correlates_to_result_inside_window(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "model", intent=True, second=0), event(c, "model", second=61)])
    q = Queries(tmp_path)
    result = q.timeseries(metric="input_tokens", bucket="1m", since="2026-10-04T10:01:00Z",
                          until="2026-10-04T10:02:00Z")
    assert result["points"][0]["value"] == 10
    assert result["legacy_calls"] == 0


def test_count_series_preserves_rejected_retries_with_same_action_id(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "retry", kind="tool_call"),
                       event(c, "retry", kind="tool_call", status="BLOCK", reason="EFFECT_REPLAY_MISMATCH")])
    q = Queries(tmp_path)
    assert series(q, "actions")["points"][0]["value"] == 2
    assert series(q, "blocked")["points"][0]["value"] == 1
    assert series(q, "input_tokens")["points"][0]["value"] == 0


def test_missing_intent_reservation_is_incomplete_not_exact_remaining(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "pending", intent=True, reserved=None)])
    result = Queries(tmp_path).session_usage(c.session_id)
    assert result["usage"]["total_tokens"] == 0
    assert result["budgets"][0]["complete"] is False
    assert result["budgets"][0]["remaining"] is None


def test_expired_corrupt_unknown_and_read_only_behavior_is_inherited(tmp_path):
    c = contract()
    path = seed(tmp_path, c, [event(c, "call")])
    q = Queries(tmp_path)
    before = path.read_bytes()
    q.session_usage(c.session_id)
    series(q)
    assert before == path.read_bytes()
    with pytest.raises(QueryError) as error:
        q.session_usage("missing")
    assert error.value.status == 404
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_runs SET lifecycle='EXPIRED'")
    with pytest.raises(QueryError) as error:
        q.session_usage(c.session_id)
    assert error.value.status == 410
    with pytest.raises(QueryError) as error:
        series(q)
    assert error.value.status == 410
    path.write_text("not sqlite")
    with pytest.raises(QueryError) as error:
        series(q)
    assert error.value.status == 503


def test_expired_rejection_is_scoped_to_requested_agent_or_session(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "call")])
    expired = contract("ses_expired", "agent_expired")
    path = seed(tmp_path, expired, [event(expired, "call")])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_runs SET lifecycle='EXPIRED'")
    q = Queries(tmp_path)
    assert series(q, "actions", agent_id=c.agent_id)["points"][0]["value"] == 1
    assert series(q, "actions", session_id=c.session_id)["points"][0]["value"] == 1
    with pytest.raises(QueryError) as error:
        series(q, "actions", agent_id=expired.agent_id)
    assert error.value.status == 410
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM events")
    # The pinned contract still establishes expiration of an empty/pruned scope.
    with pytest.raises(QueryError) as error:
        series(q, agent_id=expired.agent_id)
    assert error.value.status == 410


def test_timeseries_inherits_global_scanner_row_bound(tmp_path, monkeypatch):
    import persistence.query as query
    first, second = contract(), contract("ses_second")
    seed(tmp_path, first, [event(first, "call")])
    seed(tmp_path, second, [event(second, "call")])
    monkeypatch.setattr(query, "MAX_ROWS", 5)
    with pytest.raises(QueryError) as error:
        series(Queries(tmp_path))
    assert error.value.status == 503


def test_egress_counts_and_gateway_decisions_use_real_nonintent_records(tmp_path):
    c = contract()
    seed(tmp_path, c, [event(c, "egress", kind="egress_http", status="FAILED", decision="BLOCK"),
                       event(c, "pending", kind="egress_http", intent=True)])
    q = Queries(tmp_path)
    result = q.session_usage(c.session_id)
    assert result["egress_calls"] == {"total": 2, "blocked": 1, "pending": 1,
                                      "by_host": {"demo.example": 2}}
    assert series(q, "actions")["points"][0]["value"] == 1
    assert series(q, "blocked")["points"][0]["value"] == 1


def test_one_snapshot_per_store_for_timeseries(tmp_path, monkeypatch):
    from contextlib import contextmanager
    first, second = contract(), contract("ses_second")
    seed(tmp_path, first, [event(first, "call")])
    seed(tmp_path, second, [event(second, "call")])
    q = Queries(tmp_path)
    original, observed = q.connect, []
    @contextmanager
    def counted(path, **kwargs):
        observed.append(path)
        with original(path, **kwargs) as conn:
            yield conn
    monkeypatch.setattr(q, "connect", counted)
    series(q)
    assert len(observed) == len(set(observed)) == 2
