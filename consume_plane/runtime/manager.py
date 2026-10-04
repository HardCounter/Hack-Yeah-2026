"""ConsumerManager: receive from Queue 2, dispatch to plugins per session, settle (section 7)."""
from __future__ import annotations

import asyncio
import logging
import time
import zlib
from datetime import datetime, timezone
from typing import Callable, Sequence

from .. import __version__
from contracts.action import AgentAction
from ..model.outputs import DecisionDraft, Finding, FindingDraft, PluginDecision, decision_id, finding_id
from ..ports.event_source import Delivery, EventSource, source_is_idle
from ..ports.sinks import FindingSink
from ..ports.trajectory import TrajectoryReader
from .context import OutputBuffer, PluginContextImpl
from .feedback import FeedbackController
from .ledger import Ledger
from .metrics import Metrics
from .registry import LoadedPlugin, Registry
from tracing import get_logger

log = logging.getLogger("consume_plane.manager")
SELF_NAME = "consume-plane"


def failure_code(error: str) -> str:
    """Reduce a plugin error to a fixed code: exception type or TIMEOUT, never the message text."""
    head = error.split(":", 1)[0].strip()
    if head.startswith("timeout"):
        return "TIMEOUT"
    return head if head.isidentifier() else "PLUGIN_ERROR"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConsumerManager:
    def __init__(self, *, source: EventSource, reader: TrajectoryReader, registry: Registry, ledger: Ledger,
                 sinks: Sequence[FindingSink], feedback: FeedbackController, metrics: Metrics | None = None,
                 partitions: int = 8, plugin_timeout_s: float = 2.0, max_attempts: int = 3,
                 retry_backoff_s: float = 1.0, batch_size: int = 64, poll_timeout_s: float = 0.5,
                 clock: Callable[[], datetime] = _utcnow):
        self.source = source
        self.reader = reader
        self.registry = registry
        self.ledger = ledger
        self.sinks = list(sinks)
        self.feedback = feedback
        self.metrics = metrics or Metrics()
        self.partitions = partitions
        self.plugin_timeout_s = plugin_timeout_s
        self.max_attempts = max_attempts
        self.retry_backoff_s = retry_backoff_s
        self.batch_size = batch_size
        self.poll_timeout_s = poll_timeout_s
        self.clock = clock
        self._stop = asyncio.Event()
        self._queues: list[asyncio.Queue[Delivery]] = []
        self._last_seq: dict[str, int] = {}

    def stop(self) -> None:
        self._stop.set()

    def _partition(self, session_id: str) -> int:
        return zlib.crc32(session_id.encode()) % self.partitions

    async def _drain(self) -> None:
        await asyncio.gather(*(q.join() for q in self._queues))

    async def run(self, *, stop_when_idle: bool = False) -> None:
        """Process until stop(); with stop_when_idle, also return once a finite source is exhausted."""
        self._queues = [asyncio.Queue(maxsize=self.batch_size * 2) for _ in range(self.partitions)]
        workers = [asyncio.create_task(self._worker(q)) for q in self._queues]
        try:
            while not self._stop.is_set():
                batch = await self.source.receive(self.batch_size, self.poll_timeout_s)
                for d in batch:
                    await self._queues[self._partition(d.action.session_id)].put(d)
                if not batch and stop_when_idle:
                    await self._drain()
                    if source_is_idle(self.source):
                        break
            await self._drain()
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await self.registry.teardown_all()

    async def _worker(self, q: asyncio.Queue[Delivery]) -> None:
        while True:
            d = await q.get()
            try:
                await self._process(d)
            except Exception:
                log.exception("unexpected error processing %s", d.action.event_id)
                await self.source.nack(d.delivery_id, reason="consumer_internal_error",
                                       retry_after_s=self.retry_backoff_s)
            finally:
                q.task_done()

    def _check_order(self, a: AgentAction) -> None:
        last = self._last_seq.get(a.session_id)
        if last is not None and a.seq < last:
            log.warning("out-of-order event %s: seq %d after %d in %s", a.event_id, a.seq, last, a.session_id)
            self.metrics.inc("consumer_out_of_order_total")
        self._last_seq[a.session_id] = max(a.seq, last if last is not None else a.seq)

    async def _process(self, d: Delivery) -> None:
        a = d.action
        self._check_order(a)
        if not a.known_kind:
            self.metrics.inc("consumer_unknown_kind_total", kind=a.kind)
        pending = [p for p in self.registry.matching(a) if not self.ledger.is_settled(a.event_id, p.name, p.version)]
        outcomes = await asyncio.gather(*(self._run_one(p, a) for p in pending))

        retry: list[str] = []
        dead: list[str] = []
        for p, (buffer, error, duration_ms) in zip(pending, outcomes):
            if error is None:
                try:
                    await self._commit(p, a, buffer, attempt=d.attempt, duration_ms=duration_ms)
                except Exception as e:
                    error = f"{type(e).__name__}: commit: {e}"
            if error is None:
                self.ledger.mark_done(a.event_id, p.name, p.version, duration_ms)
                self.metrics.inc("consumer_plugin_runs_total", plugin=p.name, outcome="done")
                continue
            attempts = self.ledger.record_failure(a.event_id, p.name, p.version, error, duration_ms)
            self.metrics.inc("consumer_plugin_runs_total", plugin=p.name, outcome="failed")
            p.log.warning("failed on %s (attempt %d/%d): %s", a.event_id, attempts, self.max_attempts, error)
            dead_now = attempts >= self.max_attempts
            await self._record_failure(p, a, error, attempt=attempts, duration_ms=duration_ms,
                                       outcome="dead_lettered" if dead_now else "failed")
            if dead_now:
                self.ledger.mark_dead(a.event_id, a.session_id, p.name, p.version, attempts, error)
                self.metrics.inc("consumer_plugin_dead_letters_total", plugin=p.name)
                await self._report_plugin_failure(p, a, attempts, error)
                dead.append(p.name)
            else:
                retry.append(f"{p.name}: {error}")

        if retry:
            await self.source.nack(d.delivery_id, reason="; ".join(retry),
                                   retry_after_s=self.retry_backoff_s * 2 ** (d.attempt - 1))
            self.metrics.inc("consumer_events_total", outcome="nacked")
        else:
            await self.source.ack(d.delivery_id)
            self.metrics.inc("consumer_events_total", outcome="acked")
        get_logger().log("consume", "event.processed", session=a.session_id, event_id=a.event_id, seq=a.seq,
                         kind=a.kind, status=a.status, plugins=len(pending),
                         outcome="nacked" if retry else "acked", retry=",".join(r.split(":")[0] for r in retry) or None,
                         dead=",".join(dead) or None, attempt=d.attempt if d.attempt > 1 else None)
        ts = a.ts if a.ts.tzinfo is not None else a.ts.replace(tzinfo=timezone.utc)
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self.metrics.set("consumer_lag_seconds", (now - ts).total_seconds())

    async def _run_one(self, p: LoadedPlugin, a: AgentAction) -> tuple[OutputBuffer, str | None, float]:
        ctx = PluginContextImpl(p, a, self.reader)
        start = time.perf_counter()
        error = None
        timeout = getattr(p.instance, "timeout_s", None) or self.plugin_timeout_s  # a plugin may declare a longer bound
        try:
            await asyncio.wait_for(p.instance.handle(a, ctx), timeout)
        except TimeoutError:
            error = f"timeout after {timeout}s"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        return ctx.buffer, error, (time.perf_counter() - start) * 1000

    def _finalize(self, p_name: str, p_version: str, method: str, a: AgentAction,
                  draft: FindingDraft, index: int) -> Finding:
        return Finding(
            rule_id=draft.rule_id,
            severity=draft.severity,
            summary=draft.summary,
            evidence_event_ids=tuple(draft.evidence_event_ids),
            confidence=draft.confidence,
            details=dict(draft.details),
            finding_id=finding_id(p_name, p_version, a.event_id, draft.rule_id, index),
            plugin=p_name,
            plugin_version=p_version,
            method=method,
            run_id=a.run_id,
            session_id=a.session_id,
            agent_id=a.agent_id,
            case_id=a.case_id,
            trigger_event_id=a.event_id,
            policy_version=a.policy_version,
            created_at=self.clock(),
        )

    async def _write_findings(self, findings: list[Finding]) -> None:
        for sink in self.sinks:
            await sink.write(findings)
        for f in findings:
            self.metrics.inc("consumer_findings_total", plugin=f.plugin, severity=f.severity, rule_id=f.rule_id)

    async def _commit(self, p: LoadedPlugin, a: AgentAction, buffer: OutputBuffer, *,
                      attempt: int = 1, duration_ms: float = 0.0) -> None:
        findings = [self._finalize(p.name, p.version, p.method, a, draft, i) for i, draft in enumerate(buffer.findings)]
        if findings:
            await self._write_findings(findings)
            for f in findings:
                get_logger().log("consume", "finding", session=a.session_id, event_id=a.event_id, plugin=p.name,
                                 rule=f.rule_id, severity=f.severity)
        for name, value, labels in buffer.metrics:
            self.metrics.set(name, value, **{**labels, "plugin": p.name})
        adjustments = []
        for proposal in buffer.proposals:
            decision = await self.feedback.submit(proposal, plugin=p.name, method=p.method, action=a)
            get_logger().log("consume", "feedback.proposed", session=a.session_id, event_id=a.event_id, plugin=p.name,
                             adjustment=proposal.action, outcome=decision.reason)
            self.metrics.inc("consumer_feedback_total", plugin=p.name, outcome=decision.reason)
            adjustments.append({"action": proposal.action, "outcome": decision.reason,
                                **({"signal_id": decision.signal.signal_id} if decision.signal else {})})
        drafts = list(buffer.decisions)
        if not drafts and (findings or adjustments):
            # Every output stays traceable even when a plugin does not record its decision explicitly.
            drafts = [DecisionDraft(decision="OUTPUT_EMITTED",
                                    reasoning="plugin emitted outputs without recording a decision")]
        decisions = [PluginDecision(
            decision_id=decision_id(p.name, p.version, a.event_id, "decided", i, attempt),
            ts=self.clock(), plugin=p.name, plugin_version=p.version, method=p.method, outcome="decided",
            decision=draft.decision, reasoning=draft.reasoning, reason=None, factors=dict(draft.factors),
            session_id=a.session_id, run_id=a.run_id, agent_id=a.agent_id, case_id=a.case_id,
            trigger_event_id=a.event_id, trigger_seq=a.seq, attempt=attempt, duration_ms=round(duration_ms, 3),
            finding_ids=tuple(f.finding_id for f in findings), adjustments=tuple(adjustments),
        ) for i, draft in enumerate(drafts)]
        if decisions:
            await self._write_decisions(decisions)

    async def _write_decisions(self, decisions: list[PluginDecision]) -> None:
        for sink in self.sinks:
            write = getattr(sink, "write_decisions", None)
            if write is not None:
                await write(decisions)
        for d in decisions:
            self.metrics.inc("consumer_decisions_total", plugin=d.plugin, outcome=d.outcome, decision=d.decision)
            get_logger().log("consume", "plugin.decision", session=d.session_id, event_id=d.trigger_event_id,
                             plugin=d.plugin, outcome=d.outcome, decision=d.decision, reason=d.reason,
                             decision_id=d.decision_id)

    async def _record_failure(self, p: LoadedPlugin, a: AgentAction, error: str, *, attempt: int,
                              duration_ms: float, outcome: str) -> None:
        code = failure_code(error)
        record = PluginDecision(
            decision_id=decision_id(p.name, p.version, a.event_id, outcome, 0, attempt),
            ts=self.clock(), plugin=p.name, plugin_version=p.version, method=p.method, outcome=outcome,
            decision="PLUGIN_FAILED" if outcome == "failed" else "PLUGIN_GAVE_UP",
            reasoning=(f"attempt {attempt} of {self.max_attempts} failed ({code}); "
                       + ("event will be retried" if outcome == "failed" else "no further retries")),
            reason=code, factors={"attempt": attempt, "max_attempts": self.max_attempts},
            session_id=a.session_id, run_id=a.run_id, agent_id=a.agent_id, case_id=a.case_id,
            trigger_event_id=a.event_id, trigger_seq=a.seq, attempt=attempt, duration_ms=round(duration_ms, 3),
        )
        try:
            await self._write_decisions([record])
        except Exception:
            log.exception("could not record failure of %s on %s", p.name, a.event_id)

    async def _report_plugin_failure(self, p: LoadedPlugin, a: AgentAction, attempts: int, error: str) -> None:
        error_type = error.split(":", 1)[0]
        draft = FindingDraft(
            rule_id="consumer.plugin_failure",
            severity="medium",
            summary=f"plugin {p.name}@{p.version} gave up after {attempts} attempts ({error_type})",
            evidence_event_ids=(a.event_id,),
            details={"failed_plugin": p.name, "attempts": attempts},
        )
        try:
            await self._write_findings([self._finalize(SELF_NAME, __version__, "deterministic", a, draft, 0)])
        except Exception:
            log.exception("could not report failure of %s on %s", p.name, a.event_id)
