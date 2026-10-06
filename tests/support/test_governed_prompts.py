from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import json

import pytest

from contracts import Budget, TaskContract
from contracts.wire import decode_event
from intercept.policy.auditors import Pipeline
from intercept.governed.prompts import PromptGateway
from persistence.governed import GovernedPersistence
from persistence.store import EventStore


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def _contract(*, tokens=2000):
    return TaskContract(
        contract_id="contract_PROMPT_v1", run_id="run_prompt_1", session_id="sess_prompt_1",
        principal_id="principal_prompt_1", agent_id="onboarding-agent", case_id="APP-0001",
        role="KYC analyst", objective="Process APP-0001", target_ids=frozenset({"APP-0001"}),
        allowed_tools=frozenset({"read_application"}), postconditions=("ONB-P1",),
        budget=Budget(tokens=tokens, tool_calls=10, cost_usd=None), policy_version="policy_prompt_v1",
        policy_hash="a" * 64, feed_version="feed_prompt_v1",
    )


async def _runtime(tmp_path, *, tokens=2000, auditors=None, max_output_tokens=128):
    store = EventStore(tmp_path / "prompts.db")
    await store.initialize()
    persistence = GovernedPersistence(store)
    contract = _contract(tokens=tokens)
    await persistence.persist_contract(contract)
    specs = [
        {"id": "tool-allowlist", "type": "tool_allowlist", "config": {"allowed_tools": ["read_application"]}},
        *(auditors or []),
    ]
    gateway = PromptGateway(persistence, contract, Pipeline(specs), {"llama3.2"},
                            max_output_tokens=max_output_tokens)
    return store, persistence, gateway


TOOLS = [{"type": "function", "function": {"name": "read_application", "parameters": {"type": "object"}}}]


@pytest.mark.parametrize("part", [
    {"type": "text", "text": "safe prefix", "truncated": True, "length": 17000},
    {"type": "tool-result", "result": {"type": "omitted", "length": 17000}},
])
@async_test
async def test_incomplete_adapter_content_is_blocked_before_dispatch(tmp_path, part):
    store, persistence, gateway = await _runtime(tmp_path)
    calls = []

    async def backend(*args):
        calls.append(args)
        return {"content": "ok"}

    try:
        decision, response = await gateway.execute(
            "llama3.2", [{"role": "user", "content": [part]}], TOOLS, backend,
        )
        assert decision.decision == "BLOCK"
        assert decision.reason_code == "PROMPT_CONTENT_INCOMPLETE"
        assert response == {"error": "PROMPT_CONTENT_INCOMPLETE"}
        assert not calls
        assert (await persistence.wire_session("sess_prompt_1"))[0]["status"] == "blocked"
    finally:
        await store.close()


@async_test
async def test_allowed_prompt_call_produces_decision_and_decodeable_v21_event(tmp_path):
    store, persistence, gateway = await _runtime(tmp_path)
    calls = []

    async def backend(model, messages, tools, max_output_tokens):
        calls.append({"model": model, "messages": messages, "tools": tools, "max_output_tokens": max_output_tokens})
        return {"tool_calls": [{"function": {"name": "read_application",
                                                  "arguments": '{"app_id":"APP-0001","name":"Private Person"}'}}],
                "stop_reason": "tool_calls"}

    decision, response = await gateway.execute(
        "llama3.2", [{"role": "user", "content": "synthetic prompt"}], TOOLS, backend,
    )
    assert decision.decision == "ALLOW"
    assert len(calls) == 1
    assert calls[0]["max_output_tokens"] == 128
    wire = await persistence.wire_session("sess_prompt_1")
    assert len(wire) == 1  # durable PENDING intent has no consumer seq/delivery
    action = decode_event(wire[0])
    assert action.kind == "prompt" and action.payload.model == "llama3.2"
    assert action.payload.tool_calls_requested[0].name == "read_application"
    assert dict(action.payload.tool_calls_requested[0].args) == {"app_id": "APP-0001"}
    serialized = json.dumps(wire)
    assert "synthetic prompt" not in serialized
    assert "Private Person" not in serialized
    assert response["stop_reason"] == "tool_calls"
    await store.close()


@async_test
async def test_model_allowlist_denies_before_backend_and_persists_block(tmp_path):
    store, persistence, gateway = await _runtime(tmp_path)
    invoked = False

    async def backend(model, messages, tools, max_output_tokens):
        nonlocal invoked
        invoked = True
        return {}

    decision, result = await gateway.execute("unapproved-model", [{"role": "user", "content": "hi"}], TOOLS, backend)
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "MODEL_NOT_AUTHORIZED"
    assert result == {"error": "MODEL_NOT_AUTHORIZED"}
    assert not invoked
    event = decode_event((await persistence.wire_session("sess_prompt_1"))[0])
    assert event.status == "blocked" and event.gateway.final == "BLOCK"
    await store.close()


@async_test
async def test_prompt_token_reservation_blocks_exhausted_followup(tmp_path):
    store, persistence, gateway = await _runtime(tmp_path, tokens=350, max_output_tokens=128)
    invoked = 0

    async def backend(model, messages, tools, max_output_tokens):
        nonlocal invoked
        invoked += 1
        return {"content": "ok"}

    messages = [{"role": "user", "content": "short"}]
    first, _ = await gateway.execute("llama3.2", messages, TOOLS, backend)
    second, result = await gateway.execute("llama3.2", messages, TOOLS, backend)
    assert first.decision == "ALLOW"
    assert second.decision == "BLOCK"
    assert second.reason_code == "TOKEN_BUDGET_EXHAUSTED"
    assert invoked == 1
    events = await persistence.wire_session("sess_prompt_1")
    assert [e["status"] for e in events] == ["completed", "blocked"]
    await store.close()


@async_test
async def test_input_redaction_is_revalidated_and_sent_only_to_backend(tmp_path):
    scanner = {"id": "input-redactor", "type": "pattern_scanner",
               "config": {"patterns": ["SYNTHETIC_SECRET"], "action": "REDACT"}}
    store, persistence, gateway = await _runtime(tmp_path, auditors=[scanner])
    observed = []

    async def backend(model, messages, tools, max_output_tokens):
        observed.extend(messages)
        return {"content": "safe"}

    decision, _ = await gateway.execute(
        "llama3.2", [{"role": "user", "content": "SYNTHETIC_SECRET"}], TOOLS, backend,
    )
    assert decision.decision == "REDACT"
    assert observed[0]["content"] == "[REDACTED]"
    wire = await persistence.wire_session("sess_prompt_1")
    assert "SYNTHETIC_SECRET" not in json.dumps(wire)
    await store.close()


@async_test
async def test_output_block_withholds_body_and_records_failed_outcome(tmp_path):
    scanner = {"id": "output-blocker", "type": "pattern_scanner",
               "config": {"patterns": ["SYNTHETIC_OUTPUT_SECRET"], "action": "BLOCK"}}
    store, persistence, gateway = await _runtime(tmp_path, auditors=[scanner])

    async def backend(model, messages, tools, max_output_tokens):
        return {"content": "SYNTHETIC_OUTPUT_SECRET"}

    decision, result = await gateway.execute("llama3.2", [{"role": "user", "content": "hi"}], TOOLS, backend)
    assert decision.decision == "BLOCK"
    assert result == {"error": "OUTPUT_INSPECTION_BLOCK"}
    wire = await persistence.wire_session("sess_prompt_1")
    assert wire[-1]["status"] == "failed"
    assert "SYNTHETIC_OUTPUT_SECRET" not in json.dumps(wire)
    await store.close()


@async_test
async def test_backend_failure_has_failed_status_without_claiming_execution_success(tmp_path):
    store, persistence, gateway = await _runtime(tmp_path)

    async def backend(model, messages, tools, max_output_tokens):
        raise TimeoutError("private endpoint detail")

    decision, result = await gateway.execute("llama3.2", [{"role": "user", "content": "hi"}], TOOLS, backend)
    assert decision.decision == "ALLOW"  # authorized; execution outcome failed separately
    assert decision.reason_code == "MODEL_BACKEND_FAILED"
    assert result == {"error": "MODEL_BACKEND_FAILED"}
    event = decode_event((await persistence.wire_session("sess_prompt_1"))[-1])
    assert event.status == "failed"
    assert "private endpoint detail" not in json.dumps(event.raw)
    await store.close()


@async_test
async def test_prompt_metrics_separate_backend_latency_from_interception_overhead(tmp_path):
    store, persistence, gateway = await _runtime(tmp_path)

    async def backend(model, messages, tools, max_output_tokens):
        await asyncio.sleep(0.05)
        return {"content": "ok"}

    started = asyncio.get_running_loop().time()
    decision, _ = await gateway.execute("llama3.2", [{"role": "user", "content": "hi"}], [], backend)
    elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000
    event = (await persistence.wire_session("sess_prompt_1"))[0]
    assert event["metrics"]["latency_ms"] >= 40
    assert event["interception_metadata"]["interception_overhead_ms"] < 30
    assert decision.interception_overhead_ms < elapsed_ms - 30
    await store.close()


@async_test
async def test_prompt_admission_hook_blocks_halted_session(tmp_path):
    store = EventStore(tmp_path / "admission.db")
    await store.initialize()
    persistence = GovernedPersistence(store)
    contract = _contract()
    await persistence.persist_contract(contract)
    gateway = PromptGateway(
        persistence, contract, Pipeline([]), {"llama3.2"},
        admission_check=lambda: _halted_admission(),
    )
    invoked = False

    async def backend(model, messages, tools, max_output_tokens):
        nonlocal invoked
        invoked = True
        return {"content": "must not run"}

    decision, result = await gateway.execute("llama3.2", [{"role": "user", "content": "hi"}], [], backend)
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "SESSION_HALTED"
    assert result == {"error": "SESSION_HALTED"}
    assert not invoked
    assert (await persistence.wire_session(contract.session_id))[0]["status"] == "blocked"
    await store.close()


async def _halted_admission():
    return "SESSION_HALTED"


@async_test
async def test_session_guard_covers_backend_and_final_persistence(tmp_path):
    store = EventStore(tmp_path / "guard.db")
    await store.initialize()
    persistence = GovernedPersistence(store)
    contract = _contract()
    await persistence.persist_contract(contract)
    session_lock = asyncio.Lock()
    entered = asyncio.Event()
    release = asyncio.Event()
    guard_exited = asyncio.Event()
    finished = False

    @asynccontextmanager
    async def guard():
        nonlocal finished
        try:
            async with session_lock:
                entered.set()
                yield "SESSION_FINISHED" if finished else None
        finally:
            guard_exited.set()

    gateway = PromptGateway(persistence, contract, Pipeline([]), {"llama3.2"},
                            max_output_tokens=128, session_guard=guard)

    async def backend(model, messages, tools, max_output_tokens):
        await release.wait()
        return {"content": "done"}

    async def _finish_under_lock():
        nonlocal finished
        async with session_lock:
            finished = True

    task = asyncio.create_task(gateway.execute("llama3.2", [{"role": "user", "content": "hi"}], [], backend))
    await entered.wait()
    await asyncio.sleep(0)
    assert not guard_exited.is_set()
    assert session_lock.locked()
    finish_task = asyncio.create_task(_finish_under_lock())
    await asyncio.sleep(0)
    assert not finish_task.done() and not finished
    release.set()
    await task
    await finish_task
    assert finished
    assert guard_exited.is_set()
    decision, result = await gateway.execute("llama3.2", [{"role": "user", "content": "after finish"}], [], backend)
    assert decision.decision == "BLOCK" and result == {"error": "SESSION_FINISHED"}
    assert [e["status"] for e in await persistence.wire_session(contract.session_id)] == ["completed", "blocked"]
    await store.close()


@async_test
async def test_halt_waits_for_inflight_prompt_then_denies_next_prompt(tmp_path):
    store = EventStore(tmp_path / "halt-guard.db")
    await store.initialize()
    persistence = GovernedPersistence(store)
    contract = _contract()
    await persistence.persist_contract(contract)
    state_lock = asyncio.Lock()
    halted = False
    entered = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def guard():
        async with state_lock:
            entered.set()
            yield "SESSION_HALTED" if halted else None

    gateway = PromptGateway(persistence, contract, Pipeline([]), {"llama3.2"},
                            max_output_tokens=128, session_guard=guard)

    async def backend(model, messages, tools, max_output_tokens):
        await release.wait()
        return {"content": "done"}

    async def halt():
        nonlocal halted
        async with state_lock:
            halted = True

    prompt = asyncio.create_task(gateway.execute("llama3.2", [{"role": "user", "content": "hi"}], [], backend))
    await entered.wait()
    halt_task = asyncio.create_task(halt())
    await asyncio.sleep(0)
    assert not halt_task.done() and not halted
    release.set()
    await prompt
    await halt_task
    decision, result = await gateway.execute("llama3.2", [{"role": "user", "content": "after halt"}], [], backend)
    assert decision.decision == "BLOCK" and result == {"error": "SESSION_HALTED"}
    assert [e["status"] for e in await persistence.wire_session(contract.session_id)] == ["completed", "blocked"]
    await store.close()



@async_test
async def test_cancellation_waits_for_backend_and_persists_unknown_final_before_releasing_guard(tmp_path):
    store = EventStore(tmp_path / "cancel.db")
    await store.initialize()
    persistence = GovernedPersistence(store)
    contract = _contract()
    await persistence.persist_contract(contract)
    session_lock = asyncio.Lock()
    entered = asyncio.Event()
    backend_started = asyncio.Event()
    finish_backend = asyncio.Event()
    backend_finished = asyncio.Event()
    guard_exited = asyncio.Event()

    @asynccontextmanager
    async def guard():
        try:
            async with session_lock:
                entered.set()
                yield None
        finally:
            guard_exited.set()

    gateway = PromptGateway(persistence, contract, Pipeline([]), {"llama3.2"},
                            max_output_tokens=128, session_guard=guard)

    async def backend(model, messages, tools, max_output_tokens):
        backend_started.set()
        await finish_backend.wait()
        backend_finished.set()
        return {"content": "completed after cancellation"}

    task = asyncio.create_task(gateway.execute("llama3.2", [{"role": "user", "content": "hi"}], [], backend))
    await entered.wait()
    await backend_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not guard_exited.is_set()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not guard_exited.is_set()
    finish_backend.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend_finished.is_set()
    assert guard_exited.is_set()
    events = await persistence.wire_session(contract.session_id)
    assert len(events) == 1 and events[0]["status"] == "failed"
    assert events[0]["interception_metadata"]["final_decision"] == "ALLOW"
    assert not session_lock.locked()
    await store.close()
