import asyncio
import json
import os

import pytest

from conftest import action
from consume_plane.plugins.goal_alignment_judge import (GoalAlignmentJudge, JudgeUnavailable, OllamaBackend,
                                                        build_digest, sample_draw)
from consume_plane.runtime.config import FeedbackConfig
from contracts.task_contract import Budget, TaskContract

CONTRACT = TaskContract(
    contract_id="ctr_1", session_id="sess_1", agent_id="onboarding-agent", role="onboarding analyst",
    objective="Process application APP-0001", target_ids=frozenset({"APP-0001"}),
    allowed_tools=frozenset({"read_application", "create_client"}), postconditions=(),
    budget=Budget(tokens=20000, tool_calls=30), policy_version="v1",
)
FEEDBACK = FeedbackConfig(allowed_actions={"goal-alignment-judge": ["REQUIRE_APPROVAL_FOR"]})


class FakeOllama:
    """Scripted backend: records every call; answers with `verdict` when asked for the schema."""

    def __init__(self, verdict=None, *, available=None, tool_turns=0, fail=None, content=None):
        self.model = "fake"
        self.verdict = verdict or {"p_goal_drift": 0.05, "p_injection_influence": 0.0,
                                   "suspect_event_ids": [], "rationale_code": "ALIGNED"}
        self.availability, self.tool_turns, self.fail, self.content = available, tool_turns, fail, content
        self.availability_checks, self.calls = 0, []

    def available(self):
        self.availability_checks += 1
        return self.availability

    def chat(self, messages, *, tools=None, schema=None, temperature=0.7, max_tokens=400):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "schema": schema})
        if self.fail:
            raise self.fail
        if tools and len(self.calls) <= self.tool_turns:
            return {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "get_step", "arguments": {"event_id": "evt_sess_1_1"}}}]}, "eval_count": 5}
        return {"message": {"role": "assistant", "content": self.content or json.dumps(self.verdict)},
                "prompt_eval_count": 10, "eval_count": 5}


def judge_with(backend):
    class Judge(GoalAlignmentJudge):
        def __init__(self):
            super().__init__(backend)
    return Judge


def ended(seq):
    return action(seq, action_type="session",
                  action_details={"phase": "ended", "contract_id": "ctr_1", "policy_version": "v1"})


def session():
    """read (no trigger), create_client (write: background review), session end (final review)."""
    return [action(1, tool="read_application"),
            action(2, tool="create_client", side_effect="irreversible",
                   args={"app_id": "APP-0001", "name": "Sensitive Person"}),
            ended(3)]


def run(harness, backend, *, config=None, actions=None):
    h = harness(actions or session(), [judge_with(backend)], feedback=FEEDBACK,
                plugin_config={"goal-alignment-judge": {"sample_rate": 1.0, "samples": 1, **(config or {})}})
    h.reader.add_contract(CONTRACT)
    asyncio.run(h.run())
    return h


def no_failures(h):
    """Every event the judge subscribes to settled as done: nothing failed, retried or dead-lettered."""
    stats = h.manager.metrics
    return (stats.get("consumer_plugin_runs_total", plugin="goal-alignment-judge", outcome="failed") is None
            and stats.get("consumer_plugin_runs_total", plugin="goal-alignment-judge", outcome="done") == 3)


# --- availability and failure containment ---------------------------------------------------

def test_unavailable_ollama_only_logs_and_is_checked_once_per_ttl(harness):
    backend = FakeOllama(available="OLLAMA_UNREACHABLE")
    h = run(harness, backend)
    assert h.findings() == [] and backend.calls == []
    assert backend.availability_checks == 1          # cached as down for availability_ttl_s
    assert no_failures(h)


def test_unreachable_real_endpoint_does_not_break_the_consume_plane(harness):
    backend = OllamaBackend("http://127.0.0.1:1", "llama3.2", timeout_s=1)
    assert backend.available() == "OLLAMA_UNREACHABLE"
    h = run(harness, backend)
    assert h.findings() == [] and no_failures(h)


def test_backend_crash_during_review_is_contained(harness):
    backend = FakeOllama(fail=JudgeUnavailable("URLError"))
    h = run(harness, backend)
    assert h.findings() == [] and no_failures(h)
    assert len(backend.calls) == 1                   # marked down: the session-end review is skipped


def test_invalid_model_output_is_ignored(harness):
    h = run(harness, FakeOllama(content="I think the agent is fine."))
    assert h.findings() == [] and no_failures(h)


# --- sampling ----------------------------------------------------------------------------------

def test_sample_draw_is_deterministic_and_uniform():
    draws = [sample_draw("seed", f"evt_{i}") for i in range(10000)]
    assert draws == [sample_draw("seed", f"evt_{i}") for i in range(10000)]
    assert abs(sum(draws) / len(draws) - 0.5) < 0.02
    assert abs(sum(d < 0.3 for d in draws) / len(draws) - 0.3) < 0.02
    assert sample_draw("other-seed", "evt_1") != sample_draw("seed", "evt_1")


def test_zero_sample_rate_never_calls_the_model(harness):
    backend = FakeOllama()
    h = run(harness, backend, config={"sample_rate": 0.0, "final_sample_rate": 0.0})
    assert backend.calls == [] and backend.availability_checks == 0 and h.findings() == []


def test_review_budget_per_session(harness):
    backend = FakeOllama()
    actions = [action(i, tool="create_client", side_effect="irreversible") for i in range(1, 6)] + [ended(6)]
    run(harness, backend, config={"max_reviews_per_session": 2}, actions=actions)
    assert len(backend.calls) == 2


def test_slow_model_at_session_end_is_dropped_within_the_deadline(harness):
    import time

    class Slow(FakeOllama):
        def chat(self, messages, **kw):
            time.sleep(1.0)
            return super().chat(messages, **kw)

    durations = []
    backend = Slow({"p_goal_drift": 0.9, "p_injection_influence": 0, "suspect_event_ids": [], "rationale_code": "OTHER"})

    class Timed(GoalAlignmentJudge):
        def __init__(self):
            super().__init__(backend)

        async def handle(self, action, ctx):
            started = time.perf_counter()
            await super().handle(action, ctx)
            durations.append(time.perf_counter() - started)

    h = harness(session(), [Timed], feedback=FEEDBACK, plugin_config={"goal-alignment-judge": {
        "sample_rate": 1.0, "samples": 1, "final_timeout_s": 0.2}})
    h.reader.add_contract(CONTRACT)
    asyncio.run(h.run())
    assert max(durations) < 0.6                       # no event waited for the model beyond the deadline
    assert h.findings() == [] and no_failures(h)


def test_cancelled_background_review_does_not_kill_the_consumer(harness):
    """Regression: the simulation restarts the manager after every step. A background review that was
    cancelled in between used to raise CancelledError at session end and stall the consume plane."""
    import time

    class Slow(FakeOllama):
        def chat(self, messages, **kw):
            time.sleep(0.3)
            return super().chat(messages, **kw)

    h = harness(session()[:2], [judge_with(Slow())], feedback=FEEDBACK,
                plugin_config={"goal-alignment-judge": {"sample_rate": 1.0, "samples": 1}})
    h.reader.add_contract(CONTRACT)

    async def scenario():
        await h.run()                                 # create_client starts a background review
        judge = h.manager.registry.plugins[0].instance
        assert judge._pending and not any(t.done() for t in judge._pending.values())
        for task in judge._pending.values():
            task.cancel()
        await asyncio.sleep(0)
        end = ended(3)
        h.reader.add(end)
        h.source.put(end)
        await asyncio.wait_for(h.manager.run(stop_when_idle=True), timeout=5)

    asyncio.run(scenario())
    assert h.manager.metrics.get("consumer_plugin_runs_total", plugin="goal-alignment-judge", outcome="done") == 3


# --- verdicts ----------------------------------------------------------------------------------

def test_drift_is_reported_once_with_confidence_and_approval_proposal(harness):
    backend = FakeOllama({"p_goal_drift": 0.9, "p_injection_influence": 0.2,
                          "suspect_event_ids": ["evt_sess_1_2", "evt_unknown"], "rationale_code": "SKIPPED_CONTROL"})
    h = run(harness, backend)
    findings = h.findings("judge.goal_drift")
    assert len(findings) == 1                        # background + final review, reported on escalation only
    f = findings[0]
    assert f.severity == "high" and f.confidence == 1.0 and f.method == "semantic"
    assert f.evidence_event_ids == ("evt_sess_1_2",)  # unknown ids from the model are dropped
    assert [s.action for s in h.channel.signals] == ["REQUIRE_APPROVAL_FOR"]
    assert "Sensitive Person" not in json.dumps(f.to_dict())


def test_aligned_session_emits_metrics_but_no_finding(harness):
    h = run(harness, FakeOllama())
    assert h.findings() == [] and h.channel.signals == []
    assert h.manager.metrics.get("judge_score", session_id="sess_1", agent="onboarding-agent",
                                 plugin="goal-alignment-judge") == pytest.approx(0.05)


def test_self_consistency_lowers_confidence_when_samples_disagree():
    verdicts = iter([0.9, 0.1, 0.9])

    class Flaky(FakeOllama):
        def chat(self, messages, **kw):
            p = next(verdicts)
            return {"message": {"content": json.dumps({"p_goal_drift": p, "p_injection_influence": 0,
                                                       "suspect_event_ids": [], "rationale_code": "OTHER"})}}

    judge = GoalAlignmentJudge(Flaky())
    judge.samples, judge.max_turns, judge.max_tokens, judge.temperature = 3, 1, 100, 0.7
    v = asyncio.run(judge._review({"steps": []}, "evt", "sess"))
    assert v.score == pytest.approx((0.9 + 0.1 + 0.9) / 3)
    assert v.confidence == pytest.approx(1 - 2 * 0.3771, abs=1e-3)


# --- agentic loop ------------------------------------------------------------------------------

def test_agentic_loop_uses_tools_then_forces_a_schema_answer():
    backend = FakeOllama(tool_turns=99)              # would call tools forever
    judge = GoalAlignmentJudge(backend)
    judge.max_turns, judge.max_tokens, judge.temperature = 3, 100, 0.7
    digest = build_digest([action(1, tool="create_client", side_effect="irreversible",
                                  args={"app_id": "APP-0001", "name": "Sensitive Person"})], CONTRACT)
    parsed, _ = judge._one_sample(digest)
    assert parsed["rationale"] == "ALIGNED"
    assert len(backend.calls) == 3
    assert all(c["tools"] and not c["schema"] for c in backend.calls[:2])
    assert backend.calls[2]["tools"] is None and backend.calls[2]["schema"] is not None
    tool_results = [m["content"] for m in backend.calls[2]["messages"] if m["role"] == "tool"]
    assert len(tool_results) == 2 and '"tool": "create_client"' in tool_results[0]
    assert "Sensitive Person" not in json.dumps(backend.calls)   # only argument keys reach the model


def test_unknown_tool_calls_get_an_error_value():
    assert json.loads(GoalAlignmentJudge._run_tool("rm_rf", {}, {})) == {"error": "UNKNOWN_TOOL"}
    assert json.loads(GoalAlignmentJudge._run_tool("get_step", {"event_id": "x"}, {})) == {"error": "UNKNOWN_EVENT"}


# --- runtime: per-plugin timeout ---------------------------------------------------------------

def test_plugin_may_declare_a_longer_timeout_than_the_runtime_default(harness):
    from consume_plane.sdk import Subscription

    class Slow:
        name, version, method = "slow", "1", "deterministic"
        subscription = Subscription(kinds=frozenset({"tool_use"}), needs_trajectory=False)
        timeout_s = 1.0

        async def handle(self, action, ctx):
            await asyncio.sleep(0.1)
            ctx.emit_metric("slow_done", 1)

    h = harness([action(1)], [Slow], plugin_timeout_s=0.01)
    asyncio.run(h.run())
    assert h.manager.metrics.get("slow_done", plugin="slow") == 1


# --- optional: real local Ollama ---------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("RUN_OLLAMA_TESTS") != "1", reason="set RUN_OLLAMA_TESTS=1 with Ollama running")
def test_real_ollama_review_returns_a_valid_verdict():
    backend = OllamaBackend(os.environ.get("OLLAMA_URL", "http://localhost:11434"),
                            os.environ.get("OLLAMA_MODEL", "llama3.2"), timeout_s=120)
    assert backend.available() is None
    judge = GoalAlignmentJudge(backend)
    judge.samples, judge.max_turns, judge.max_tokens, judge.temperature = 1, 2, 400, 0.2
    v = asyncio.run(judge._review(build_digest(session()[:2], CONTRACT), "evt_sess_1_2", "sess_1"))
    assert v is not None and 0 <= v.score <= 1
