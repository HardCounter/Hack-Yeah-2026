"""Unit tests of the decision-trace model: IDs, failure codes, records and sinks."""
import asyncio
import json

import pytest

from conftest import action
from consume_plane.adapters.jsonl import JsonlSink
from consume_plane.adapters.persistence import PersistenceFindingSink
from consume_plane.model.outputs import PluginDecision, decision_id
from consume_plane.runtime.manager import failure_code
from consume_plane.sdk import FindingDraft, Subscription
from persistence.privacy import decision_projection
from persistence.store import EventStore


def record(**overrides):
    from datetime import datetime, timezone
    base = dict(decision_id="dec_x", ts=datetime(2026, 10, 4, 10, tzinfo=timezone.utc), plugin="p", plugin_version="1",
                method="deterministic", outcome="decided", decision="NO_CHANGE", reasoning="score 0.12 (low)",
                reason=None, factors={"score": 0.12, "level": "low"}, session_id="sess_1", run_id="run_1",
                agent_id="onboarding-agent", case_id="APP-0001", trigger_event_id="evt_1", trigger_seq=1,
                attempt=1, duration_ms=0.5, finding_ids=("fnd_1",),
                adjustments=({"action": "ALERT", "outcome": "accepted", "signal_id": "sig_1"},))
    return PluginDecision(**{**base, **overrides})


# --- identifiers and codes -----------------------------------------------------------------------

def test_decision_id_is_deterministic_and_distinguishes_every_component():
    base = ("p", "1", "evt_1", "decided", 0, 1)
    assert decision_id(*base) == decision_id(*base)
    variants = {decision_id(*base[:i], "x" if isinstance(base[i], str) else base[i] + 1, *base[i + 1:])
                for i in range(len(base))}
    assert len(variants | {decision_id(*base)}) == len(base) + 1
    assert decision_id(*base).startswith("dec_") and len(decision_id(*base)) == 28


@pytest.mark.parametrize(("error", "code"), [
    ("ValueError: Jan Kowalski", "ValueError"),
    ("timeout after 2.0s", "TIMEOUT"),
    ("RuntimeError: commit: sink down", "RuntimeError"),
    ("weird message without type", "PLUGIN_ERROR"),
    ("", "PLUGIN_ERROR"),
])
def test_failure_code_never_keeps_the_message(error, code):
    assert failure_code(error) == code


# --- the record crosses the consume -> persistence boundary unchanged ------------------------------

def test_record_serializes_and_survives_the_privacy_projection():
    d = record().to_dict()
    json.dumps(d)
    safe = decision_projection(d)
    for key in ("decision_id", "plugin", "decision", "reasoning", "factors", "finding_ids", "adjustments",
                "session_id", "trigger_event_id", "outcome", "attempt"):
        assert safe[key] == d[key], key
    assert safe["ts"] == "2026-10-04T10:00:00.000000Z"


# --- sinks ------------------------------------------------------------------------------------------

def test_jsonl_sink_writes_decisions_next_to_findings(tmp_path):
    sink = JsonlSink(tmp_path / "runs" / "{run_id}" / "findings.jsonl")
    asyncio.run(sink.write_decisions([record(), record(decision_id="dec_y", decision="LEVEL_RAISED")]))
    lines = (tmp_path / "runs" / "run_1" / "decisions.jsonl").read_text().splitlines()
    assert [json.loads(line)["decision"] for line in lines] == ["NO_CHANGE", "LEVEL_RAISED"]


def test_persistence_sink_round_trip(tmp_path):
    async def go():
        store = EventStore(tmp_path / "evidence.db")
        await store.initialize()
        sink = PersistenceFindingSink(store)
        await sink.write_decisions([record(), record()])        # replay: stored once
        await sink.write_decisions([record(decision_id="dec_f", outcome="failed", decision="PLUGIN_FAILED",
                                           reason="TimeoutError", finding_ids=(), adjustments=())])
        rows = await sink.decisions("sess_1")
        await store.close()
        return rows
    rows = asyncio.run(go())
    assert [(r["decision_id"], r["outcome"], r["reason"]) for r in rows] == [
        ("dec_x", "decided", None), ("dec_f", "failed", "TimeoutError")]


# --- manager behaviour --------------------------------------------------------------------------------

class TwoDecisions:
    name, version, method = "two", "1", "deterministic"
    subscription = Subscription(kinds=frozenset({"tool_use"}), needs_trajectory=False)

    async def handle(self, action, ctx):
        ctx.record_decision("CHECK_A_PASSED", "a ok", a=1)
        ctx.emit_finding(FindingDraft(rule_id="two.b", severity="low", summary="s", evidence_event_ids=(action.event_id,)))
        ctx.record_decision("CHECK_B_FLAGGED", "b flagged", b=2)


def test_several_decisions_in_one_run_share_the_run_outputs(harness):
    h = harness([action(1)], [TwoDecisions])
    asyncio.run(h.run())
    decisions = sorted(h.sink.decisions.values(), key=lambda d: d.decision)
    assert [d.decision for d in decisions] == ["CHECK_A_PASSED", "CHECK_B_FLAGGED"]
    assert len({d.decision_id for d in decisions}) == 2
    finding = h.findings()[0].finding_id
    assert all(d.finding_ids == (finding,) for d in decisions)


class FlakyOnce:
    name, version, method = "flaky", "1", "deterministic"
    subscription = Subscription(kinds=frozenset({"tool_use"}), needs_trajectory=False)
    calls = 0

    async def handle(self, action, ctx):
        type(self).calls += 1
        if type(self).calls == 1:
            raise ConnectionError("upstream down")
        ctx.record_decision("RECOVERED", "second attempt succeeded")


def test_retry_trace_shows_the_failed_attempt_then_the_decision(harness):
    FlakyOnce.calls = 0
    h = harness([action(1)], [FlakyOnce])
    asyncio.run(h.run())
    trace = sorted(h.sink.decisions.values(), key=lambda d: d.attempt)
    assert [(d.attempt, d.outcome, d.decision, d.reason) for d in trace] == [
        (1, "failed", "PLUGIN_FAILED", "ConnectionError"), (2, "decided", "RECOVERED", None)]


class Silent:
    name, version, method = "silent", "1", "deterministic"
    subscription = Subscription(kinds=frozenset({"tool_use"}), needs_trajectory=False)

    async def handle(self, action, ctx):
        ctx.emit_metric("x", 1)


def test_runs_without_decisions_or_outputs_leave_no_trace(harness):
    h = harness([action(1)], [Silent])
    asyncio.run(h.run())
    assert h.sink.decisions == {}
