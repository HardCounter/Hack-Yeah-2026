import asyncio
import time

from conftest import action
from consume_plane.sdk import FindingDraft, Subscription


def plugin(name, *, kinds=("tool_use",), method="deterministic", **sub_kw):
    """Build a plugin class whose handle() delegates to `self.fn`, recording what it saw."""
    def decorate(fn):
        cls = type(name.title().replace("-", ""), (), {
            "name": name, "version": "1.0.0", "method": method,
            "subscription": Subscription(kinds=frozenset(kinds), **sub_kw),
            "seen": [],
        })

        async def handle(self, a, ctx):
            type(self).seen.append(a.event_id)
            await fn(self, a, ctx)

        cls.handle = handle
        return cls
    return decorate


def test_subscription_filters_kinds_tools_and_unknown_kinds(harness):
    @plugin("tools-only", tools=frozenset({"create_client"}))
    async def tools_only(self, a, ctx):
        pass

    @plugin("everything", kinds=("*",))
    async def everything(self, a, ctx):
        pass

    actions = [action(1, tool="read_application"), action(2, tool="create_client"),
               action(3, action_type="llm_call"), action(4, action_type="a2a_message")]
    h = harness(actions, [tools_only, everything])
    asyncio.run(h.run())
    assert tools_only.seen == ["evt_sess_1_2"]
    assert everything.seen == [a.event_id for a in actions]
    assert h.manager.metrics.get("consumer_unknown_kind_total", kind="a2a_message") == 1
    assert h.source.acked == [a.event_id for a in actions]


def test_trajectory_is_a_snapshot_as_of_the_handled_event(harness):
    seen_lengths = {}

    @plugin("snap")
    async def snap(self, a, ctx):
        seen_lengths[a.seq] = [x.seq for x in await ctx.trajectory()]

    actions = [action(i) for i in range(1, 4)]
    h = harness(actions, [snap])
    asyncio.run(h.run())
    # the reader already holds all three events; each snapshot stops at the handled one
    assert seen_lengths == {1: [1], 2: [1, 2], 3: [1, 2, 3]}


def test_content_requires_needs_content(harness):
    outcomes = {}

    @plugin("no-content")
    async def no_content(self, a, ctx):
        try:
            await ctx.content(a.payload.result)
        except PermissionError:
            outcomes["no-content"] = "denied"

    @plugin("with-content", needs_content=True)
    async def with_content(self, a, ctx):
        outcomes["with-content"] = await ctx.content(a.payload.result)

    a = action(1)
    h = harness([a], [no_content, with_content])
    h.reader.add_content(a.payload.result.ref, b"OCR text")
    asyncio.run(h.run())
    assert outcomes == {"no-content": "denied", "with-content": b"OCR text"}


def test_redelivery_reruns_only_failed_plugins_and_does_not_duplicate_findings(harness):
    @plugin("steady")
    async def steady(self, a, ctx):
        ctx.emit_finding(FindingDraft(rule_id="steady.seen", severity="low", summary="seen"))

    @plugin("flaky")
    async def flaky(self, a, ctx):
        ctx.emit_finding(FindingDraft(rule_id="flaky.partial", severity="low", summary="never committed"))
        if len(type(self).seen) == 1:
            raise RuntimeError("transient")
        ctx.emit_finding(FindingDraft(rule_id="flaky.ok", severity="low", summary="ok"))

    h = harness([action(1)], [steady, flaky])
    asyncio.run(h.run())
    assert steady.seen == ["evt_sess_1_1"]                 # not rerun on redelivery
    assert flaky.seen == ["evt_sess_1_1", "evt_sess_1_1"]
    assert h.source.nacks[0][0] == "evt_sess_1_1" and "flaky" in h.source.nacks[0][1]
    assert h.source.acked == ["evt_sess_1_1"]
    assert sorted(f.rule_id for f in h.findings()) == ["flaky.ok", "flaky.partial", "steady.seen"]
    assert h.ledger.state("evt_sess_1_1", "flaky", "1.0.0") == ("done", 2)


def test_broken_plugin_is_dead_lettered_without_blocking_others(harness):
    @plugin("broken")
    async def broken(self, a, ctx):
        raise ValueError("bug")

    @plugin("fine")
    async def fine(self, a, ctx):
        pass

    h = harness([action(1)], [broken, fine], max_attempts=3)
    asyncio.run(h.run())
    assert len(broken.seen) == 3 and fine.seen == ["evt_sess_1_1"]
    assert h.source.acked == ["evt_sess_1_1"] and h.source.dead_letters == []
    assert [d["plugin"] for d in h.ledger.dead_letters()] == ["broken"]
    [failure] = h.findings("consumer.plugin_failure")
    assert failure.plugin == "consume-plane" and failure.details["failed_plugin"] == "broken"
    assert "bug" not in failure.summary                    # exception text stays in the ledger only


def test_slow_plugin_times_out_without_stalling_other_sessions(harness):
    @plugin("slow")
    async def slow(self, a, ctx):
        if a.session_id == "sess_slow":
            await asyncio.sleep(5)

    actions = [action(1, session_id="sess_slow")] + [action(i, session_id="sess_fast") for i in range(1, 4)]
    h = harness(actions, [slow], plugin_timeout_s=0.2, max_attempts=1)
    start = time.perf_counter()
    asyncio.run(h.run())
    assert time.perf_counter() - start < 2
    assert [d["event_id"] for d in h.ledger.dead_letters()] == ["evt_sess_slow_1"]
    assert {"evt_sess_fast_1", "evt_sess_fast_2", "evt_sess_fast_3"} <= set(h.source.acked)


def test_same_session_is_sequential_and_sessions_run_in_parallel(harness):
    log = []

    @plugin("ordered")
    async def ordered(self, a, ctx):
        log.append(("start", a.session_id, a.seq))
        await asyncio.sleep(0.05)
        log.append(("end", a.session_id, a.seq))

    sessions = ["sess_a", "sess_b", "sess_c", "sess_d"]
    actions = [action(i, session_id=s) for i in range(1, 4) for s in sessions]
    h = harness(actions, [ordered], partitions=16)
    start = time.perf_counter()
    asyncio.run(h.run())
    elapsed = time.perf_counter() - start
    for s in sessions:
        events = [(kind, seq) for kind, sid, seq in log if sid == s]
        assert events == [("start", 1), ("end", 1), ("start", 2), ("end", 2), ("start", 3), ("end", 3)]
    assert elapsed < 12 * 0.05            # sequential would take 0.6 s; sessions overlap


def test_semantic_findings_need_confidence_and_are_capped(harness):
    @plugin("judge", method="semantic")
    async def judge(self, a, ctx):
        if a.seq == 1:
            ctx.emit_finding(FindingDraft(rule_id="judge.drift", severity="critical", summary="x", confidence=0.7))
        else:
            ctx.emit_finding(FindingDraft(rule_id="judge.no_conf", severity="low", summary="x"))

    h = harness([action(1), action(2)], [judge], max_attempts=1)
    asyncio.run(h.run())
    [drift] = h.findings("judge.drift")
    assert drift.severity == "high" and drift.method == "semantic"
    assert h.findings("judge.no_conf") == []
    assert [d["event_id"] for d in h.ledger.dead_letters()] == ["evt_sess_1_2"]


def test_metrics_from_plugins_are_labelled(harness):
    @plugin("counter")
    async def counter(self, a, ctx):
        ctx.emit_metric("calls_seen", a.seq, agent=a.agent_id)

    h = harness([action(1), action(2)], [counter])
    asyncio.run(h.run())
    assert h.manager.metrics.get("calls_seen", agent="onboarding-agent", plugin="counter") == 2
