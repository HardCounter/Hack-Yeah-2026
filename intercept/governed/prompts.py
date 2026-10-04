"""Policy-gated local LLM calls for governed sessions.

PromptGateway is deliberately separate from the direct local simulation chat
helper. Callers supply a trusted backend callback; operator configuration
selects the allowed model names. Prompt and completion bodies are inspected in
memory and never copied to the general evidence envelope.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping, Sequence

from contracts import DECISIONS, GatewayDecision, TaskContract
from persistence import ActionEventEnvelope, ActionStatus, ActionType
from persistence.events import build_action_event
from persistence.vocabulary import is_intent, storage_status
from tracing import get_logger

_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class PromptGateway:
    """Synchronous authorization, budget reservation, inspection and evidence for LLM calls.

    One instance is scoped to one immutable TaskContract. The caller must keep
    its backend callback trusted and local (the runtime uses its Ollama adapter).
    """

    def __init__(self, persistence: Any, contract: TaskContract, pipeline: Any,
                 allowed_models: frozenset[str] | set[str] | Sequence[str], *,
                 max_output_tokens: int = 1024,
                 admission_check: Callable[[], Awaitable[str | None]] | None = None,
                 session_guard: Callable[[], Any] | None = None):
        if not isinstance(contract, TaskContract):
            raise TypeError("contract must be contracts.TaskContract")
        if not callable(getattr(persistence, "append", None)) or not callable(getattr(persistence, "intent", None)):
            raise TypeError("persistence must provide governed append and intent APIs")
        models = frozenset(allowed_models)
        if not models or any(not isinstance(model, str) or not _MODEL_NAME.fullmatch(model) for model in models):
            raise ValueError("allowed_models must be a non-empty set of operator-approved model IDs")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192:
            raise ValueError("max_output_tokens must be an integer from 1 to 8192")
        self.persistence = persistence
        self.contract = contract
        self.allowed_models = models
        self.max_output_tokens = max_output_tokens
        if admission_check is not None and not callable(admission_check):
            raise TypeError("admission_check must be an async callable")
        self.admission_check = admission_check
        if session_guard is not None and not callable(session_guard):
            raise TypeError("session_guard must be a context-manager factory")
        self.session_guard = session_guard
        # Prompt calls still run configured input/output scanners and webhooks,
        # but tool allowlist auditors govern tool dispatch, not model selection.
        specs = getattr(pipeline, "specs", None)
        if specs is not None and hasattr(pipeline, "evaluate"):
            from intercept.policy.auditors import Pipeline
            self.pipeline = (pipeline.for_prompts() if isinstance(pipeline, Pipeline)
                             else Pipeline([s for s in specs if s.get("type") != "tool_allowlist"]))
        else:
            self.pipeline = pipeline
        self._lock = asyncio.Lock()
        self._used_tokens: int | None = None

    @staticmethod
    def _json_copy(value: Any, *, label: str, max_bytes: int = 65536) -> Any:
        try:
            raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
            if len(raw.encode("utf-8")) > max_bytes:
                raise ValueError
            return json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            raise ValueError(f"{label} is not bounded JSON data") from None

    @staticmethod
    def _status_for_decision(decision: str) -> ActionStatus:
        return storage_status(decision)

    async def _load_usage(self) -> int:
        if self._used_tokens is not None:
            return self._used_tokens
        used = 0
        # Count durable dispatch intents so a process restart after model
        # dispatch but before its result cannot recover the reserved budget.
        store = getattr(self.persistence, "store", None)
        events = (await store.get_events_by_session(self.contract.session_id)
                  if store is not None else [])
        for event in events:
            if event.action_type != ActionType.LLM_INVOCATION:
                continue
            if is_intent(event):
                reserved = event.context.reserved_usage.get("reserved_tokens", 0)
                if type(reserved) is int and reserved >= 0:
                    used += reserved
        self._used_tokens = used
        return used

    async def _evaluate(self, *, call_id: str, arguments: dict[str, Any], phase="input"):
        action = {
            "session_id": self.contract.session_id,
            "call_id": call_id,
            "tool": "llm_call",
            "arguments": arguments,
        }
        if callable(getattr(self.pipeline, "evaluate_prompt", None)):
            return await self.pipeline.evaluate_prompt(action, phase=phase)
        return await self.pipeline.evaluate(action)

    @staticmethod
    def _decision_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
        return tuple({
            "auditor": str(row.get("auditor", "unknown")),
            "decision": str(row.get("decision", "ALLOW")),
            "rule_id": row.get("rule_id") or (str(row.get("code")) if row.get("code") else None),
            "latency_ms": float(row.get("latency_ms", 0.0)),
            "evidence": {k: v for k, v in row.get("evidence", {}).items()
                         if k in {"tokens_used", "tokens_limit", "tool_calls_used", "tool_calls_limit"}
                         and type(v) is int},
        } for row in rows if row.get("decision") in DECISIONS)

    @staticmethod
    def _final_decision(verdict: str, changed: bool, rows: Sequence[Mapping[str, Any]]) -> str:
        if verdict in ("BLOCK", "REQUIRE_APPROVAL"):
            return verdict
        if changed:
            return "REDACT"
        if any(row.get("decision") == "ALERT" for row in rows):
            return "ALERT"
        return "ALLOW"

    def _decision(self, action_id: str, name: str, reason: str | None,
                  rows: Sequence[Mapping[str, Any]], started: float,
                  backend_elapsed_ms: float = 0.0) -> GatewayDecision:
        return GatewayDecision(
            action_id=action_id,
            decision=name,
            reason_code=reason,
            policy_version=self.contract.policy_version,
            auditor_decisions=self._decision_rows(rows),
            modified_arguments=None,
            interception_overhead_ms=max(0.0, (time.perf_counter() - started) * 1000 - backend_elapsed_ms),
        )

    @staticmethod
    def _requested_tools(response: Mapping[str, Any], known_tools: frozenset[str]) -> list[dict[str, Any]]:
        raw_calls = response.get("tool_calls") or response.get("tool_calls_requested") or ()
        if not isinstance(raw_calls, (list, tuple)):
            return []
        requested = []
        for call in raw_calls[:64]:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function", call)
            if not isinstance(function, Mapping):
                continue
            name = function.get("name")
            if not isinstance(name, str) or name not in known_tools:
                continue
            parameters = function.get("arguments", function.get("parameters", {}))
            if isinstance(parameters, str):
                try:
                    parameters = json.loads(parameters)
                except (ValueError, TypeError):
                    parameters = {}
            safe_parameters = {}
            if isinstance(parameters, Mapping):
                # Only preserve the application target needed for trajectory scope.
                app_id = parameters.get("app_id", parameters.get("application_id"))
                if isinstance(app_id, str) and _MODEL_NAME.fullmatch(app_id):
                    safe_parameters["app_id"] = app_id
            requested.append({"name": name, "parameters": safe_parameters})
        return requested

    @staticmethod
    def _known_tool_names(tools: Sequence[Any]) -> frozenset[str]:
        names = set()
        for tool in tools:
            if not isinstance(tool, Mapping):
                continue
            fn = tool.get("function", tool)
            if isinstance(fn, Mapping) and isinstance(fn.get("name"), str):
                names.add(fn["name"])
        return frozenset(names)

    def _event(self, *, action_id: str, model: str, decision: str, status: ActionStatus,
               reason: str | None, rows: Sequence[Mapping[str, Any]], latency_ms: float,
               backend_latency_ms: float = 0.0,
               input_bound: int = 0, output_bound: int = 0,
               requested: Sequence[Mapping[str, Any]] = (), stop_reason: str | None = None,
               intent: bool = False) -> ActionEventEnvelope:
        """One model-request record. Prompt and completion bodies are never included."""
        return build_action_event(
            contract=self.contract, action_type="llm_call", action_id=action_id, name=model,
            status=status, decision=decision, intent=intent, auditor_rows=rows,
            latency_ms=latency_ms, reason_code=reason, event_id=f"evt_{uuid.uuid4().hex}",
            wire_details={
                "model": model,
                "provider": "ollama",
                "messages": [],
                "completion": None,
                "tool_calls_requested": list(requested),
                "stop_reason": stop_reason,
            },
            reserved_usage={"reserved_tokens": input_bound + output_bound},
            # Conservative charge estimates unless trusted usage is returned by the backend callback.
            actual_usage={"input_tokens": input_bound, "output_tokens": output_bound,
                          "latency_ms": max(0.0, backend_latency_ms)},
        )

    async def execute(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        backend: Callable[..., Awaitable[Any]],
    ) -> tuple[GatewayDecision, Any]:
        """Authorize one local model attempt, then inspect both request and response.

        `backend` is a trusted async adapter called positionally as
        `(model, checked_messages, checked_tools, max_output_tokens)`.
        """
        trace = get_logger()
        session = self.contract.session_id
        trace.log("intercept", "prompt.received", session=session, kind="prompt",
                  model=model if isinstance(model, str) else None,
                  messages=len(messages) if isinstance(messages, Sequence) else None)
        try:
            decision, response = await self._execute_guarded(model, messages, tools, backend)
        except BaseException as exc:
            trace.log("intercept", "prompt.error", session=session, error=type(exc).__name__)
            raise
        trace.log("intercept", "prompt.decided", session=session, action=decision.action_id,
                  decision=decision.decision, reason=decision.reason_code,
                  overhead_ms=round(decision.interception_overhead_ms, 2))
        return decision, response

    async def _execute_guarded(self, model, messages, tools, backend) -> tuple[GatewayDecision, Any]:
        if self.session_guard is not None:
            async with self.session_guard() as admission_reason:
                return await self._execute_admitted(model, messages, tools, backend, admission_reason)
        if self.admission_check is not None:
            try:
                admission_reason = await self.admission_check()
            except Exception:
                admission_reason = "SESSION_ADMISSION_UNAVAILABLE"
        else:
            admission_reason = None
        return await self._execute_admitted(model, messages, tools, backend, admission_reason)

    async def _execute_admitted(
        self, model: str, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]],
        backend: Callable[..., Awaitable[Any]], admission_reason: str | None,
    ) -> tuple[GatewayDecision, Any]:
        """Run a prompt while the trusted session guard remains held."""
        async with self._lock:
            started = time.perf_counter()
            action_id = f"act_{uuid.uuid4().hex}"
            rows: Sequence[Mapping[str, Any]] = ()
            final = "BLOCK"
            reason: str | None = None
            status = ActionStatus.BLOCKED
            response: Any = {"error": "MODEL_NOT_AUTHORIZED"}
            input_bound = output_bound = 0
            backend_elapsed_ms = 0.0
            input_redacted = False
            cancelled = False

            if admission_reason:
                reason = admission_reason if admission_reason in {
                    "UNKNOWN_SESSION", "SESSION_HALTED", "SESSION_FINISHED", "STRICT_MODE_LLM_DENIED",
                    "DYNAMIC_LLM_RESTRICTION", "SESSION_ADMISSION_UNAVAILABLE",
                } else "SESSION_ADMISSION_DENIED"
                response = {"error": reason}

            if reason is not None:
                pass
            elif not isinstance(model, str) or model not in self.allowed_models:
                reason = "MODEL_NOT_AUTHORIZED"
            elif self.contract.budget.cost_usd not in (None, 0, 0.0):
                reason = "LOCAL_MODEL_COST_BUDGET_UNSUPPORTED"
                denied = self.pipeline.report_budget_limit({
                    "session_id": self.contract.session_id, "call_id": action_id,
                    "tool": "llm_call", "arguments": {},
                }, "COST_BUDGET_UNSUPPORTED") if callable(getattr(self.pipeline, "report_budget_limit", None)) else None
                rows = (denied,) if denied else ()
            elif self.contract.budget.tokens is None:
                reason = "TOKEN_BUDGET_REQUIRED"
            else:
                try:
                    copied_messages = self._json_copy(messages, label="messages")
                    copied_tools = self._json_copy(tools, label="tools")
                    if not isinstance(copied_messages, list) or not isinstance(copied_tools, list):
                        raise ValueError
                    if len(copied_messages) > 64 or len(copied_tools) > 64:
                        raise ValueError
                except ValueError:
                    reason = "INVALID_PROMPT_ARGUMENTS"
                else:
                    input_bound = max(1, len(json.dumps(
                        {"messages": copied_messages, "tools": copied_tools},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode("utf-8")))
                    output_bound = self.max_output_tokens
                    used = await self._load_usage()
                    token_limit = self.contract.budget.tokens
                    if used + input_bound + output_bound > token_limit:
                        reason = "TOKEN_BUDGET_EXHAUSTED"
                        denied = self.pipeline.report_budget_limit({
                            "session_id": self.contract.session_id, "call_id": action_id,
                            "tool": "llm_call", "arguments": {},
                        }, reason) if callable(getattr(self.pipeline, "report_budget_limit", None)) else None
                        rows = (denied,) if denied else ()
                    else:
                        checked, rows, verdict, changed = await self._evaluate(
                            call_id=action_id,
                            arguments={"messages": copied_messages, "tools": copied_tools},
                        )
                        final = self._final_decision(verdict, changed, rows)
                        input_redacted = changed
                        reason = next((str(r["code"]) for r in rows if r.get("decision") in ("BLOCK", "REQUIRE_APPROVAL", "ALERT")), None)
                        copied_messages = checked["arguments"].get("messages", copied_messages)
                        copied_tools = checked["arguments"].get("tools", copied_tools)
                        if final not in ("BLOCK", "REQUIRE_APPROVAL"):
                            budget_row = self.pipeline.reserve_budget(
                                {"session_id": self.contract.session_id, "call_id": action_id,
                                 "tool": "llm_call", "arguments": {"messages": copied_messages,
                                                                        "tools": copied_tools}},
                                action_type="llm_call", tokens=input_bound + output_bound,
                                known_tokens=used,
                            ) if callable(getattr(self.pipeline, "reserve_budget", None)) else None
                            if budget_row:
                                rows = tuple(rows) + (budget_row,)
                                if budget_row["decision"] != "ALLOW":
                                    final = "BLOCK"
                                    reason = budget_row["code"]
                        if final in ("BLOCK", "REQUIRE_APPROVAL"):
                            status = self._status_for_decision(final)
                            response = {"error": reason or final}
                        else:
                            # Charge the entire reservation before calling the backend;
                            # failures and ambiguous timeouts do not refund it.
                            self._used_tokens = used + input_bound + output_bound
                            intent_event = self._event(
                                action_id=action_id, model=model, decision=final,
                                status=ActionStatus.PENDING, reason="MODEL_DISPATCH_INTENT",
                                rows=rows, latency_ms=(time.perf_counter() - started) * 1000,
                                input_bound=input_bound, output_bound=output_bound, intent=True,
                            )
                            await self.persistence.intent(intent_event)
                            backend_task = asyncio.create_task(
                                backend(model, copied_messages, copied_tools, self.max_output_tokens)
                            )
                            try:
                                backend_started = time.perf_counter()
                                raw = await asyncio.shield(backend_task)
                                backend_elapsed_ms = (time.perf_counter() - backend_started) * 1000
                                trusted_usage: Mapping[str, Any] = {}
                                if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], Mapping):
                                    raw, trusted_usage = raw
                                response = self._json_copy(raw, label="model response")
                                if not isinstance(response, Mapping):
                                    raise ValueError("invalid response shape")
                            except asyncio.CancelledError:
                                # Shielding fences cancellation from a to_thread-backed
                                # runtime call. Wait for its definitive completion while
                                # the session dispatch lock remains held, then persist an
                                # unknown/failed outcome before propagating cancellation.
                                cancelled = True
                                while not backend_task.done():
                                    try:
                                        await asyncio.shield(backend_task)
                                    except asyncio.CancelledError:
                                        continue
                                    except Exception:
                                        break
                                try:
                                    backend_task.result()
                                except BaseException:
                                    pass
                                backend_elapsed_ms = (time.perf_counter() - backend_started) * 1000
                                final, status, reason = "ALLOW", ActionStatus.FAILED, "MODEL_CALL_CANCELLED"
                                response = {"error": "MODEL_CALL_CANCELLED"}
                            except Exception:
                                final, status, reason = "ALLOW", ActionStatus.FAILED, "MODEL_BACKEND_FAILED"
                                response = {"error": "MODEL_BACKEND_FAILED"}
                            else:
                                inspected, output_rows, output_verdict, output_changed = await self._evaluate(
                                    call_id=action_id,
                                    arguments={"messages": [], "tools": [], "tool_result": response},
                                    phase="output",
                                )
                                rows = tuple(rows) + tuple({**r, "phase": "output"} for r in output_rows)
                                output_final = self._final_decision(output_verdict, output_changed, output_rows)
                                # Input redaction remains visible in the final decision even if
                                # the output scan made no further changes.
                                final = ("REDACT" if input_redacted and output_final == "ALLOW"
                                         else output_final)
                                reason = next((str(r["code"]) for r in output_rows
                                               if r.get("decision") in ("BLOCK", "REQUIRE_APPROVAL", "ALERT")), None)
                                if final in ("BLOCK", "REQUIRE_APPROVAL"):
                                    final, status, reason = "BLOCK", ActionStatus.FAILED, reason or "OUTPUT_INSPECTION_BLOCK"
                                    response = {"error": "OUTPUT_INSPECTION_BLOCK"}
                                else:
                                    status = ActionStatus.REDACTED if final == "REDACT" else ActionStatus.EXECUTED
                                    response = inspected["arguments"].get("tool_result", response)

            if final == "BLOCK" and reason is None:
                reason = "MODEL_NOT_AUTHORIZED"
            if isinstance(response, dict) and reason and response.get("error") == "MODEL_NOT_AUTHORIZED" and reason != "MODEL_NOT_AUTHORIZED":
                response = {"error": reason}
            decision = self._decision(action_id, final, reason, rows, started, backend_elapsed_ms)
            requested = self._requested_tools(response, self._known_tool_names(tools) if isinstance(tools, Sequence) else frozenset()) if isinstance(response, Mapping) else []
            event = self._event(
                action_id=action_id, model=model if isinstance(model, str) and _MODEL_NAME.fullmatch(model) else "unapproved_model",
                decision=final, status=status, reason=reason, rows=rows,
                latency_ms=decision.interception_overhead_ms,
                backend_latency_ms=backend_elapsed_ms,
                input_bound=input_bound, output_bound=output_bound,
                requested=requested,
                stop_reason=(response.get("stop_reason") if isinstance(response, Mapping) and
                             response.get("stop_reason") in {"tool_calls", "stop", "length", "end_turn",
                                                              "max_tokens", "content_filter"} else "unknown"),
            )
            await self.persistence.append(event)
            if cancelled:
                raise asyncio.CancelledError
            return decision, response
