"""Governance REST contracts backed by synthetic, trusted-writer SQLite evidence."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from configuration.service import ConfigService
from contracts import Budget, PolicyAdjustmentSignal, TaskContract
from persistence.events import build_action_event
from persistence.governed import GovernedPersistence
from persistence.http_api import create_app
from persistence.store import EventStore


PRIVATE = "synthetic private body must never leave evidence storage"
SINCE = "2026-10-04T10:00:30Z"
UNTIL = "2026-10-04T10:02:30Z"


def api_client(directory, config_dir):
    # Even if a caller enters TestClient's lifespan, config state stays temporary.
    return TestClient(create_app(evidence_dir=directory,
                                 config_service=ConfigService(config_dir)))


def seed_governance(directory, session):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.evidence.db"

    async def write():
        store = EventStore(path)
        await store.initialize()
        boundary = GovernedPersistence(store)
        contract = TaskContract(
            contract_id=f"contract_{session}", run_id=f"run_{session}", session_id=session,
            principal_id="principal_synthetic", agent_id=f"agent_{session}", case_id=f"case_{session}",
            role="synthetic analyst", objective=PRIVATE, target_ids=frozenset({"APP-TEST"}),
            allowed_tools=frozenset({"read_application", "create_client", "erase_record"}),
            postconditions=("ONB-P1",), budget=Budget(tokens=1000, tool_calls=20),
            policy_version="policy_synthetic", policy_hash="b" * 64, feed_version="feed_synthetic")
        await boundary.persist_contract(contract)

        async def event(label, action_type, name, status, ts, **kwargs):
            action_id = kwargs.pop("action_id", f"act_{session}_{label}")
            candidate = build_action_event(
                contract=contract, action_type=action_type, action_id=action_id,
                event_id=f"evt_{session}_{label}", name=name, status=status,
                parameters={"app_id": "APP-TEST", "raw_body": PRIVATE}, **kwargs)
            candidate.ts = ts
            candidate.action_details.error = PRIVATE if status == "FAILED" else None
            return await (boundary.intent(candidate) if kwargs.get("intent") else boundary.append(candidate))

        await event("start", "session", "session_started", "EXECUTED", "2026-10-04T10:00:00Z",
                    wire_details={"phase": "started"})
        for label, name, status, side_effect, decision in (
            ("read", "read_application", "ALLOW", "read", "ALLOW"),
            ("deny", "create_client", "BLOCK", "write", "BLOCK"),
            ("redact", "erase_record", "REDACT", "irreversible", "REDACT"),
            ("approval", "create_client", "REQUIRE_APPROVAL", "write", "REQUIRE_APPROVAL"),
            ("tool_failed", "read_application", "FAILED", "read", "ALERT"),
        ):
            await event(label, "tool_call", name, status, SINCE, side_effect=side_effect,
                        decision=decision)

        # PromptGateway persists positive bounds even on a rejected proposal. A
        # dispatched output-inspection block is FAILED, not an undispatched BLOCKED.
        for label, status, decision, ts, inputs, outputs, dispatched, reason in (
            ("model_ok", "EXECUTED", "ALLOW", "2026-10-04T10:01:00Z", 10, 3, True, None),
            ("model_failed", "FAILED", "ALLOW", "2026-10-04T10:01:30Z", 20, 5, True, "MODEL_BACKEND_FAILED"),
            ("output_blocked", "FAILED", "BLOCK", "2026-10-04T10:02:00Z", 30, 7, True, "OUTPUT_INSPECTION_BLOCK"),
            ("proposal_blocked", "BLOCK", "BLOCK", UNTIL, 900, 80, False, "TOKEN_BUDGET_EXCEEDED"),
        ):
            common = {"action_id": f"act_{session}_{label}",
                      "actual_usage": {"input_tokens": inputs, "output_tokens": outputs,
                                       "latency_ms": 12, "raw_body": PRIVATE},
                      "reserved_usage": {"reserved_tokens": inputs + outputs}}
            if dispatched:
                await event(f"{label}_intent", "llm_call", "model_synthetic", "PENDING", ts,
                            intent=True, **common)
            await event(label, "llm_call", "model_synthetic", status, ts,
                        decision=decision, reason_code=reason, **common)
        await event("unresolved", "llm_call", "model_synthetic", "PENDING", "2026-10-04T10:03:00Z",
                    intent=True, reserved_usage={"reserved_tokens": 47},
                    actual_usage={"input_tokens": 40, "output_tokens": 7})
        await event("egress", "egress_http", "synthetic.example", "ALLOW", "2026-10-04T10:03:00Z",
                    wire_details={"method": "GET", "host": "synthetic.example", "path": "/status"})

        # Shape matches the runtime's PolicyAdjustmentSignal; the trusted writer
        # removes its free-text reason before storing the signal projection.
        for label, action, applied, ts, ttl in (
            ("proposed", "ALERT", False, datetime(2026, 10, 4, 10, 4, tzinfo=timezone.utc), 60),
            ("expired", "BLOCK_TOOLS", True, datetime(2020, 1, 1, tzinfo=timezone.utc), 60),
            ("active", "STRICT_MODE", True, datetime.now(timezone.utc) - timedelta(minutes=1), 3600),
        ):
            signal_id = f"sig_{session}_{label}"
            signal = PolicyAdjustmentSignal(
                signal_id=signal_id, ts=ts, target_scope={"session_id": session}, action=action,
                policy_modifications={"tools": ["create_client"]} if action == "BLOCK_TOOLS" else {},
                reason=PRIVATE, ttl_seconds=ttl, source_plugin="trajectory-risk",
                trigger_event_id=f"evt_{session}_deny")
            await boundary.persist_signal(signal)
            if applied:
                await event(f"control_{label}", "control", "adjustment_applied", "EXECUTED",
                            ts.isoformat(), intervention_id=signal_id,
                            wire_details={"change": "adjustment_applied", "signal_id": signal_id})
                await boundary.mark_signal_applied(signal_id)
        await store.close()

    asyncio.run(write())
    return path


@pytest.fixture
def governance(tmp_path):
    directory = tmp_path / "evidence"
    paths = {session: seed_governance(directory, session) for session in ("ses_a", "ses_b")}
    return api_client(directory, tmp_path / "configs"), paths


def successful(client, url, **params):
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["x-data-source"] == "persisted"
    assert PRIVATE not in response.text
    return response.json()


def ids(page, key="event_id"):
    return [item[key] for item in page["items"]]


@pytest.mark.parametrize("filters,labels", [
    ({"session_id": "ses_a"}, None),
    ({"run_id": "run_ses_a"}, None),
    ({"case_id": "case_ses_a"}, None),
    ({"agent_id": "agent_ses_a"}, None),
    ({"action_id": "act_ses_a_model_ok"}, ["model_ok"]),
    ({"kinds": "tool_use", "statuses": "completed"}, ["read"]),
    ({"kinds": "prompt", "statuses": "failed"}, ["model_failed", "output_blocked"]),
    ({"kinds": "tool_use,prompt", "statuses": "completed,redacted", "decisions": "ALLOW,REDACT"},
     ["read", "redact", "model_ok"]),
    ({"decisions": "REQUIRE_APPROVAL"}, ["approval"]),
    ({"decisions": "ALERT"}, ["tool_failed"]),
    ({"name": "erase_record"}, ["redact"]),
    ({"side_effects": "write,irreversible"}, ["deny", "redact", "approval"]),
    ({"kinds": "prompt", "side_effects": "read"}, []),
    ({"since": "2026-10-04T10:01:00Z", "until": "2026-10-04T10:02:00Z"}, ["model_ok", "model_failed"]),
    ({"since": "2026-10-04T10:01:00.000001Z", "until": "2026-10-04T10:02:00Z"}, ["model_failed"]),
    ({"name": "absent_tool"}, []),
])
def test_actions_filters(governance, filters, labels):
    client, _ = governance
    params = {"session_id": "ses_a", **filters}
    page = successful(client, "/api/v1/actions", **params)
    assert all(item["session_id"] == "ses_a" for item in page["items"])
    if labels is not None:
        assert ids(page) == [f"evt_ses_a_{label}" for label in labels]
    else:
        assert len(page["items"]) == 13
    assert all(item["status"] != "pending" for item in page["items"])


def test_actions_include_intents_and_cross_store_tied_timestamp_pagination(governance):
    client, _ = governance
    whole = successful(client, "/api/v1/actions", include_intents=True)
    assert len(whole["items"]) == 34
    intents = [item for item in whole["items"] if item["status"] == "pending"]
    assert len(intents) == 8 and all(item["seq"] is None for item in intents)
    related = successful(client, "/api/v1/actions", action_id="act_ses_a_model_ok", include_intents=True)
    assert set(ids(related)) == {"evt_ses_a_model_ok_intent", "evt_ses_a_model_ok"}
    seen, cursor = [], None
    for _ in range(40):
        params = {"include_intents": True, "limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = successful(client, "/api/v1/actions", **params)
        seen.extend(ids(page))
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
        assert cursor
    assert seen == ids(whole)
    assert len(seen) == len(set(seen))
    tied = successful(client, "/api/v1/actions", since=SINCE, until="2026-10-04T10:00:31Z")
    assert len(tied["items"]) == 10
    for session in ("ses_a", "ses_b"):
        assert [item["event_id"] for item in tied["items"] if item["session_id"] == session] == [
            f"evt_{session}_{label}" for label in ("read", "deny", "redact", "approval", "tool_failed")]


@pytest.mark.parametrize("changed", [{"include_intents": True}, {"session_id": "ses_a"},
                                      {"statuses": "blocked"}, {"since": SINCE}])
def test_actions_cursor_binds_filters(governance, changed):
    client, _ = governance
    first = successful(client, "/api/v1/actions", limit=1)
    response = client.get("/api/v1/actions", params={"limit": 1, "cursor": first["next_cursor"], **changed})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_actions_removed_cursor_anchor(governance):
    client, paths = governance
    first = successful(client, "/api/v1/actions", session_id="ses_a", limit=1)
    with sqlite3.connect(paths["ses_a"]) as conn:
        conn.execute("DELETE FROM events WHERE event_id=?", (ids(first)[0],))
    response = client.get("/api/v1/actions", params={"session_id": "ses_a", "limit": 1,
                                                   "cursor": first["next_cursor"]})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_cursor"


def test_interventions_persisted_proposed_applied_expired_and_control_links(governance):
    client, _ = governance
    page = successful(client, "/api/v1/interventions", session_id="ses_a")
    rows = {item["signal_id"]: item for item in page["items"]}
    assert len(rows) == 3
    proposed, expired, active = [rows[f"sig_ses_a_{label}"] for label in ("proposed", "expired", "active")]
    assert proposed["applied"] is False and proposed["active"] is False
    assert proposed["applied_event_id"] is None
    assert expired["applied"] is True and expired["active"] is False
    assert active["applied"] is True and active["active"] is True
    for label, item in (("expired", expired), ("active", active)):
        assert item["applied_event_id"] == f"evt_ses_a_control_{label}"
        assert item["trigger_event_id"] == "evt_ses_a_deny"
        assert item["reason"] is None
        control = successful(client, "/api/v1/actions", session_id="ses_a", kinds="control",
                             name="adjustment_applied", action_id=f"act_ses_a_control_{label}")
        assert ids(control) == [item["applied_event_id"]]
    assert expired["tools"] == ["create_client"]


@pytest.mark.parametrize("filters,labels", [
    ({"applied": False}, ["proposed"]), ({"active": True}, ["active"]),
    ({"active": False}, ["expired", "proposed"]),
    ({"actions": "BLOCK_TOOLS"}, ["expired"]),
    ({"source_plugin": "absent-plugin"}, []),
    ({"source_plugin": "trajectory-risk", "applied": False}, ["proposed"]),
    ({"since": "2026-10-04T10:04:00Z", "until": "2026-10-04T10:04:01Z"}, ["proposed"]),
    ({"until": "2026-10-04T10:04:00Z", "actions": "ALERT"}, []),
])
def test_intervention_filters(governance, filters, labels):
    client, _ = governance
    page = successful(client, "/api/v1/interventions", session_id="ses_a", **filters)
    assert set(ids(page, "signal_id")) == {f"sig_ses_a_{label}" for label in labels}


def test_interventions_pagination_cursor_binding_and_removed_anchor(governance):
    client, paths = governance
    whole = successful(client, "/api/v1/interventions")
    seen, cursor = [], None
    for _ in range(7):
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        page = successful(client, "/api/v1/interventions", **params)
        seen.extend(ids(page, "signal_id"))
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert seen == ids(whole, "signal_id") and len(set(seen)) == 6
    first = successful(client, "/api/v1/interventions", session_id="ses_a", limit=1)
    params = {"session_id": "ses_a", "limit": 1, "cursor": first["next_cursor"]}
    changed = client.get("/api/v1/interventions", params={**params, "applied": False})
    assert changed.status_code == 400 and changed.json()["error"]["code"] == "invalid_cursor"
    with sqlite3.connect(paths["ses_a"]) as conn:
        conn.execute("DELETE FROM policy_signals WHERE signal_id=?", (ids(first, "signal_id")[0],))
    removed = client.get("/api/v1/interventions", params=params)
    assert removed.status_code == 400 and removed.json()["error"]["code"] == "invalid_cursor"


def test_session_usage_charges_dispatches_once_not_blocked_proposals(governance):
    client, _ = governance
    result = successful(client, "/api/v1/sessions/ses_a/usage")
    assert result["session_id"] == "ses_a"
    assert result["usage"]["input_tokens"] == 60
    assert result["usage"]["output_tokens"] == 15
    assert result["usage"]["total_tokens"] == 75
    assert result["usage"]["source"] == "estimated"
    # Four resolved proposals plus one unresolved dispatch, counted once each.
    assert result["model_calls"]["total"] == 5
    assert result["model_calls"]["pending"] == 1
    assert result["model_calls"]["completed"] == 1
    assert result["model_calls"]["failed"] == 2
    # Includes the output-blocked dispatch, which also counts as failed.
    assert result["model_calls"]["blocked"] == 2
    assert result["tool_calls"]["total"] == 5
    assert result["egress_calls"]["total"] == 1
    tokens = next(item for item in result["budgets"] if item["resource"] == "tokens")
    assert tokens["limit"] == 1000 and tokens["used"] == 75
    assert tokens["reserved"] == 47
    assert tokens["remaining"] == 878
    assert tokens["exceeded"] is False
    assert tokens["scope"] == "session"
    assert tokens["utilisation"] == pytest.approx(0.122)
    for budget in result["budgets"]:
        assert {"resource", "limit", "used", "reserved", "remaining", "utilisation", "exceeded", "scope"} <= budget.keys()
    # Tool dispatch reservations and cost pricing are not reconstructible from
    # these events; deliberately do not assert invented exact balances for them.


@pytest.mark.parametrize("metric,values", [
    ("actions", [6, 2]), ("blocked", [1, 1]), ("redacted", [1, 0]),
    ("input_tokens", [10, 50]), ("output_tokens", [3, 12]),
])
def test_timeseries_utc_partial_buckets_and_dispatch_accounting(governance, metric, values):
    client, _ = governance
    result = successful(client, "/api/v1/metrics/timeseries", metric=metric, bucket="1m",
                        session_id="ses_a", since="2026-10-04T12:00:30+02:00", until=UNTIL)
    assert result["metric"] == metric and result["bucket"] == "1m"
    assert [point["ts"] for point in result["points"]] == [
        "2026-10-04T10:00:30.000000Z", "2026-10-04T10:01:30.000000Z"]
    assert [point["value"] for point in result["points"]] == values
    all_sessions = successful(client, "/api/v1/metrics/timeseries", metric=metric, bucket="1m",
                              since=SINCE, until=UNTIL)
    assert [point["value"] for point in all_sessions["points"]] == [2 * value for value in values]
    by_agent = successful(client, "/api/v1/metrics/timeseries", metric=metric, bucket="1m",
                          agent_id="agent_ses_a", since=SINCE, until=UNTIL)
    assert by_agent["points"] == result["points"]


@pytest.mark.parametrize("metric", ["actions", "blocked", "redacted", "input_tokens", "output_tokens"])
def test_timeseries_empty_store_zero_fills(tmp_path, metric):
    directory = tmp_path / "empty"
    directory.mkdir()
    client = api_client(directory, tmp_path / "configs")
    result = successful(client, "/api/v1/metrics/timeseries", metric=metric, bucket="1m",
                        since=SINCE, until=UNTIL)
    assert len(result["points"]) == 2
    assert all(point["value"] == 0 for point in result["points"])


def test_empty_action_and_intervention_lists_are_not_examples(tmp_path):
    directory = tmp_path / "empty"
    directory.mkdir()
    client = api_client(directory, tmp_path / "configs")
    for url in ("/api/v1/actions", "/api/v1/interventions"):
        page = successful(client, url)
        assert page == {"items": [], "next_cursor": None, "has_more": False}


def test_timeseries_half_open_microsecond_window(governance):
    client, _ = governance
    params = {"metric": "input_tokens", "bucket": "1m", "session_id": "ses_a",
              "since": "2026-10-04T10:01:00Z", "until": "2026-10-04T10:02:00Z"}
    included = successful(client, "/api/v1/metrics/timeseries", **params)
    assert [point["value"] for point in included["points"]] == [30]
    excluded = successful(client, "/api/v1/metrics/timeseries",
                          **{**params, "since": "2026-10-04T10:01:00.000001Z"})
    assert [point["value"] for point in excluded["points"]] == [20]


@pytest.mark.parametrize("bucket,seconds", [("1m", 60), ("5m", 300), ("1h", 3600), ("1d", 86400)])
def test_timeseries_1000_point_ceiling_includes_partial_bucket(tmp_path, bucket, seconds):
    directory = tmp_path / "empty"
    directory.mkdir()
    client = api_client(directory, tmp_path / "configs")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=seconds * 1000)
    params = {"metric": "actions", "bucket": bucket, "since": start.isoformat(), "until": end.isoformat()}
    assert len(successful(client, "/api/v1/metrics/timeseries", **params)["points"]) == 1000
    too_wide = client.get("/api/v1/metrics/timeseries", params={**params,
                          "until": (end + timedelta(microseconds=1)).isoformat()})
    assert too_wide.status_code == 400 and too_wide.json()["error"]["code"] == "bad_request"
    # Buckets are anchored at since, so shifting both bounds preserves the count.
    unaligned = client.get("/api/v1/metrics/timeseries", params={**params,
                           "since": (start + timedelta(seconds=1)).isoformat(),
                           "until": (end + timedelta(seconds=1)).isoformat()})
    assert unaligned.status_code == 200
    assert len(unaligned.json()["points"]) == 1000


@pytest.mark.parametrize("metric", ["detections", "cost_usd", "interception_overhead_ms_p95"])
def test_unsupported_timeseries_metrics_are_not_fabricated(governance, metric):
    client, _ = governance
    response = client.get("/api/v1/metrics/timeseries", params={"metric": metric, "since": SINCE, "until": UNTIL})
    assert response.status_code == 501 and response.json()["error"]["code"] == "not_implemented"


@pytest.mark.parametrize("url,params,code", [
    ("/api/v1/actions", {"kinds": "invalid"}, "invalid_filter"),
    ("/api/v1/actions", {"statuses": "invalid"}, "invalid_filter"),
    ("/api/v1/actions", {"decisions": "allow"}, "invalid_filter"),
    ("/api/v1/actions", {"side_effects": "invalid"}, "invalid_filter"),
    ("/api/v1/actions", {"limit": 0}, "bad_request"),
    ("/api/v1/actions", {"limit": 1001}, "bad_request"),
    ("/api/v1/actions", {"cursor": "not-a-cursor"}, "invalid_cursor"),
    ("/api/v1/interventions", {"actions": "ALLOW"}, "invalid_filter"),
    ("/api/v1/interventions", {"applied": "maybe"}, "bad_request"),
    ("/api/v1/metrics/timeseries", {"metric": "actions", "bucket": "2m"}, "invalid_filter"),
    ("/api/v1/metrics/timeseries", {"metric": "actions", "since": "2026-10-04T10:00:00"}, "bad_request"),
    ("/api/v1/metrics/timeseries", {"metric": "actions", "since": UNTIL, "until": SINCE}, "bad_request"),
])
def test_governance_invalid_parameters(governance, url, params, code):
    client, _ = governance
    response = client.get(url, params=params)
    assert response.status_code == 400 and response.json()["error"]["code"] == code


def test_usage_unknown_expired_and_corrupt_sessions_are_distinct(governance, tmp_path):
    client, paths = governance
    unknown = client.get("/api/v1/sessions/unknown/usage")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    with sqlite3.connect(paths["ses_a"]) as conn:
        conn.execute("UPDATE audit_runs SET lifecycle='EXPIRED'")
    expired = client.get("/api/v1/sessions/ses_a/usage")
    assert expired.status_code == 410 and expired.json()["error"]["code"] == "evidence_expired"
    bad_dir = tmp_path / "broken"
    bad_dir.mkdir()
    (bad_dir / "ses_broken.evidence.db").write_text(PRIVATE)
    broken = api_client(bad_dir, tmp_path / "bad-configs")
    corrupt = broken.get("/api/v1/sessions/ses_broken/usage")
    assert corrupt.status_code == 503 and corrupt.json()["error"]["code"] == "store_unavailable"
    assert PRIVATE not in corrupt.text
    for url, params in (("/api/v1/actions", {}), ("/api/v1/interventions", {}),
                        ("/api/v1/metrics/timeseries", {"metric": "actions", "since": SINCE, "until": UNTIL})):
        response = broken.get(url, params=params)
        assert response.status_code == 503 and response.json()["error"]["code"] == "store_unavailable"
        assert PRIVATE not in response.text


def test_governance_reads_are_read_only_and_reapply_privacy(governance):
    client, paths = governance
    with sqlite3.connect(paths["ses_a"]) as conn:
        row = conn.execute("SELECT payload_json FROM events WHERE event_id='evt_ses_a_model_failed'").fetchone()
        payload = json.loads(row[0])
        payload["action_details"]["error"] = PRIVATE
        payload["context"]["actual_usage"]["raw_body"] = PRIVATE
        conn.execute("UPDATE events SET payload_json=? WHERE event_id='evt_ses_a_model_failed'", (json.dumps(payload),))
        row = conn.execute("SELECT signal_json FROM policy_signals WHERE signal_id='sig_ses_a_active'").fetchone()
        signal = json.loads(row[0])
        signal["reason"] = PRIVATE
        conn.execute("UPDATE policy_signals SET signal_json=? WHERE signal_id='sig_ses_a_active'", (json.dumps(signal),))
    before = {session: path.read_bytes() for session, path in paths.items()}
    requests = [("/api/v1/actions", {"include_intents": True}),
                ("/api/v1/interventions", {}), ("/api/v1/sessions/ses_a/usage", {}),
                ("/api/v1/metrics/timeseries", {"metric": "input_tokens", "since": SINCE, "until": UNTIL})]
    for url, params in requests:
        successful(client, url, **params)
        denied = client.post(url, json={"raw_body": PRIVATE})
        assert denied.status_code == 405 and denied.json()["error"]["code"] == "method_not_allowed"
        assert PRIVATE not in denied.text
    assert {session: path.read_bytes() for session, path in paths.items()} == before


def test_new_panels_read_actual_offline_gateway_demo(tmp_path):
    from persistence.http_api.demo import build_demo

    demo = build_demo(tmp_path / "demo")
    client = api_client(Path(demo["evidence_dir"]), tmp_path / "configs")
    for key in ("allowed_session", "blocked_session"):
        session = demo[key]
        result = successful(client, f"/api/v1/sessions/{session}/usage")
        assert result["model_calls"]["total"] == 0
        assert result["usage"]["total_tokens"] == 0
        assert result["usage"]["cost_usd"] is None
        tools = successful(client, "/api/v1/actions", session_id=session, kinds="tool_use")
        assert result["tool_calls"]["total"] == len(tools["items"])
        window = result["window"]
        until = datetime.fromisoformat(window["last_ts"].replace("Z", "+00:00")) + timedelta(seconds=1)
        chart = successful(client, "/api/v1/metrics/timeseries", metric="actions", bucket="1m",
                           session_id=session, since=window["first_ts"], until=until.isoformat())
        assert sum(point["value"] for point in chart["points"]) == len(tools["items"])
    assert successful(client, f"/api/v1/sessions/{demo['blocked_session']}/usage")["tool_calls"]["blocked"] > 0
