"""Trusted in-process adapters for the governed interception facade.

The adapter owns invocation of the synthetic tool registry. Callers cannot pick a
bank path or registry identity through an ActionProposal.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Callable, Protocol


class RegistryLike(Protocol):
    REGISTRY: dict[str, Any]
    AGENT_TOOLS: dict[str, list[str]]

    def call(self, name: str, args: dict[str, Any], ctx: Any, *,
             before_execute: Callable[..., None] | None = None,
             before_commit: Callable[..., None] | None = None,
             after_audit: Callable[..., None] | None = None,
             audit_projection: Callable[..., Any] | None = None,
             gateway_action_id: str | None = None, durable: bool = False) -> dict[str, Any]: ...

    def openai_tools(self, names: list[str] | None = None) -> list[dict[str, Any]]: ...


class RegistryCallCancelled(asyncio.CancelledError):
    """Cancellation observed after the worker finished, carrying its known result."""

    def __init__(self, result: dict[str, Any]):
        super().__init__("registry call completed while caller was cancelled")
        self.result = result


class RegistryAdapter:
    """Calls `registry.call` under the gateway's trusted identity and DB context."""

    def __init__(self, registry_module: RegistryLike):
        self.registry = registry_module

    def context(self, ctx: Any, *, session_id: str, agent_id: str, database: Any) -> Any:
        """Return a registry Ctx pinned to the trusted run's bank path and identity."""
        if getattr(ctx, "session_id", None) != session_id or getattr(ctx, "agent", None) != agent_id:
            raise ValueError("registry context conflicts with governed run binding")
        if not hasattr(ctx, "db"):
            raise ValueError("registry context has no pinned database")
        try:
            return replace(ctx, session_id=session_id, agent=agent_id, db=database)
        except TypeError:
            # Small test/third-party context objects can be immutable but not dataclasses.
            clone = object.__new__(type(ctx))
            clone.__dict__.update(vars(ctx))
            clone.session_id, clone.agent, clone.db = session_id, agent_id, database
            return clone

    async def execute(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        ctx: Any,
        before_execute: Callable[[Any, Any, str, dict[str, Any]], None] | None = None,
        before_commit: Callable[[Any, Any, str, dict[str, Any], dict[str, Any]], None] | None = None,
        after_audit: Callable[..., None] | None = None,
        audit_projection: Callable[..., Any] | None = None,
        gateway_action_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one registered tool off-loop, with trusted transaction callbacks."""
        task = asyncio.create_task(asyncio.to_thread(
            self.registry.call, name, arguments, ctx,
            before_execute=before_execute, before_commit=before_commit,
            after_audit=after_audit, audit_projection=audit_projection, gateway_action_id=gateway_action_id,
            durable=True,
        ))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # SQLite continues in the worker thread after cancellation. Keep the
                # session lock held until its transaction has a known completion.
                cancelled = True
        result = task.result()
        if cancelled:
            raise RegistryCallCancelled(result)
        return result

    async def catalog(self, *, agent_id: str, allowed_tools: set[str]) -> list[dict[str, Any]]:
        names = sorted(set(self.registry.AGENT_TOOLS.get(agent_id, ())) & allowed_tools)
        return await asyncio.to_thread(self.registry.openai_tools, names)


class InProcessFeedbackChannel:
    """Capability wrapper for trusted in-process consumer feedback.

    The receiver and credential are supplied by the trusted runtime. This module
    deliberately has no dependency on consumer implementations.
    """

    def __init__(self, receiver: Any, credential: object):
        if not callable(getattr(receiver, "apply_signal", None)):
            raise TypeError("feedback receiver must expose apply_signal")
        if credential is None:
            raise ValueError("feedback credential is required")
        self._receiver = receiver
        self._credential = credential

    async def send(self, signal: Any) -> None:
        await self._receiver.apply_signal(signal, source=self._credential)

    async def publish(self, signal: Any) -> None:
        """Consumer-boundary spelling; delegates to the same authenticated receiver."""
        await self.send(signal)
