"""Trusted local composition of the three existing runtime layers.

The synchronous simulation owns one asyncio loop. It drains committed deliveries
after each action, so demo feedback is applied before the next proposal.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import uuid

from contracts import ActionProposal, Budget, TaskContract
from intercept.policy.auditors import Pipeline
from intercept.policy.runs import Policy
from persistence.store import EventStore
from persistence.governed import GovernedPersistence
from consume_plane.runtime.config import ConsumePlaneConfig, FeedbackConfig, PluginEntry, SourceConfig
from consume_plane.runtime.bootstrap import build_manager
from consume_plane.runtime.registry import LoadedPlugin
from consume_plane.ports.plugin import SetupContext

POLICY_PATH = Path(__file__).with_name("policy.json")


class GovernedRuntime:
    """Composition root; only this trusted runtime issues authority and identity."""

    def __init__(self, ctx, app_id, *, policy_path=POLICY_PATH, policy_config=None, contract_id=None):
        self._configure(ctx, app_id, policy_path, policy_config, contract_id)
        self.runner = asyncio.Runner()
        try:
            self.runner.run(self._start())
        except BaseException:
            if hasattr(self, "store"):
                self.runner.run(self.store.close())
            self.runner.close()
            raise

    def _configure(self, ctx, app_id, policy_path, policy_config, contract_id):
        self.ctx = ctx
        self.app_id = app_id
        self.config = json.loads(Path(policy_path).read_text()) if policy_config is None else json.loads(json.dumps(policy_config))
        self.operator_contract_id = contract_id
        self.closed = False
        self.decisions = []

    @classmethod
    async def create(cls, ctx, app_id, *, policy_path=POLICY_PATH, policy_config=None, contract_id=None):
        """Async composition for the HTTP/OpenCode execution adapter."""
        runtime = cls.__new__(cls)
        runtime._configure(ctx, app_id, policy_path, policy_config, contract_id)
        runtime.runner = None
        try:
            await runtime._start()
        except BaseException:
            if hasattr(runtime, "store"):
                await runtime.store.close()
            raise
        return runtime

    async def _start(self):
        from intercept.governed.gateway import GovernedGateway, InProcessFeedbackChannel
        from intercept.governed.prompts import PromptGateway
        from consume_plane.adapters.persistence import PersistenceEventSource, PersistenceTrajectoryReader, PersistenceFindingSink
        from consume_plane.plugins.outcome_verifier import OutcomeVerifier
        from consume_plane.plugins.trajectory_risk import TrajectoryRisk
        from consume_plane.plugins.gateway_violations import GatewayViolations
        import registry

        self.audit_path = Path(self.ctx.db).with_name(f"{self.ctx.session_id}.evidence.db")
        self.store = EventStore(str(self.audit_path))
        await self.store.initialize()
        self.persistence = GovernedPersistence(self.store)
        identity = registry.AGENT_TOOLS.get(self.ctx.agent)
        if not identity:
            raise ValueError("unknown governed agent")
        ceiling = self.config["admin_tools"] if self.ctx.agent == "admin-agent" else self.config["allowed_tools"]
        allowed = [name for name in ceiling if name in identity]
        if not allowed:
            raise ValueError("agent has no tools under the pinned policy")
        core = {"runs": {self.ctx.session_id: {
            "contract_id": self.operator_contract_id or f"contract_{self.ctx.session_id}",
            "allowed_tools": allowed,
            "tool_call_budget": self.config["budget"]["tool_calls"],
            "require_approval": self.config["require_approval"],
            "argument_equals": {name: {"app_id": self.app_id} for name in allowed
                                if "app_id" in registry.REGISTRY[name].args},
        }}}
        policy = Policy(core)
        # Pin the full central configuration, including controls and consumer thresholds.
        import hashlib
        policy.version = hashlib.sha256(json.dumps(self.config, sort_keys=True, allow_nan=False).encode()).hexdigest()
        self.contract = TaskContract(
            contract_id=core["runs"][self.ctx.session_id]["contract_id"],
            run_id=f"run_{self.ctx.session_id}", session_id=self.ctx.session_id,
            principal_id="principal_local_simulator", agent_id=self.ctx.agent,
            case_id=self.app_id, role="KYC onboarding analyst", objective=f"Process application {self.app_id}",
            target_ids=frozenset({self.app_id}), allowed_tools=frozenset(allowed),
            postconditions=tuple(f"ONB-P{i}" for i in range(1, 7)), budget=Budget(**self.config["budget"]),
            policy_version=policy.version, policy_hash=policy.version, feed_version=self.config["feed_version"],
        )
        credential = object()
        self._feedback_credential = credential
        self.gateway = GovernedGateway(policy, Pipeline(self.config["auditors"]), self.persistence,
                                       registry_module=registry, feedback_credential=credential,
                                       controls=self.config.get("controls") or {})
        self.source = PersistenceEventSource(self.persistence)
        await self.source.start()
        self.reader = PersistenceTrajectoryReader(self.persistence)
        self.sink = PersistenceFindingSink(self.persistence)
        cfg = ConsumePlaneConfig(
            source=SourceConfig(poll_timeout_s=0.01), partitions=1,
            ledger_path=str(self.audit_path.with_suffix(".ledger.db")), retry_backoff_s=0.01,
            feedback=FeedbackConfig(**self.config["consumer"]["feedback"]),
            plugins={"trajectory-risk": PluginEntry(name="trajectory-risk", config=self.config["consumer"]["trajectory_risk"])},
        )
        self.manager = await build_manager(cfg, source=self.source, reader=self.reader, sinks=[self.sink],
                                           channel=InProcessFeedbackChannel(self.gateway, credential), extra_plugins=[TrajectoryRisk, GatewayViolations])
        self.verifier = OutcomeVerifier(bank_paths={self.ctx.session_id: Path(self.ctx.db)}, result_store=self.store)
        await self.verifier.setup(SetupContext(self.verifier.name, {}, self.manager.registry.plugins[0].log))
        self.manager.registry.plugins.append(LoadedPlugin(self.verifier, self.verifier.name, self.verifier.version,
                                                         self.verifier.method, self.verifier.subscription, "trusted-runtime"))
        await self.gateway.start(self.contract, self.ctx)
        self.prompt_gateway = PromptGateway(self.persistence, self.contract, self.gateway.pipeline,
                                            self.config["allowed_models"], max_output_tokens=self.config["max_output_tokens"],
                                            session_guard=lambda: self.gateway.prompt_guard(self.contract.session_id))
        await self.settle()

    async def settle(self):
        await self.manager.run(stop_when_idle=True)

    async def _execute(self, name, args, fault_injected):
        proposal = ActionProposal(action_id=f"act_{uuid.uuid4().hex}", session_id=self.contract.session_id,
                                  agent_id=self.contract.agent_id, tool=name, arguments=args,
                                  side_effect=self.gateway.registry.REGISTRY[name].side_effect if name in self.gateway.registry.REGISTRY else "read")
        decision, result = await self.gateway.execute(proposal, fault_injected=fault_injected)
        self.decisions.append(decision)
        await self.settle()
        return result

    def execute(self, name, args, *, fault_injected=False):
        return self.runner.run(self._execute(name, args, fault_injected))

    async def _prompt(self, model, messages, tools, backend):
        from urllib.parse import urlsplit
        import agent
        destination = urlsplit(agent.OLLAMA_URL)
        if destination.scheme != "http" or destination.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("governed simulation requires an operator-selected local Ollama endpoint")
        async def dispatch(selected_model, checked_messages, checked_tools, output_cap):
            task = asyncio.create_task(asyncio.to_thread(backend, selected_model, checked_messages, checked_tools,
                                                       max_tokens=output_cap))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A thread cannot be cancelled: fence it before releasing session admission.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                try:
                    task.result()
                except BaseException:
                    pass
                raise
        decision, result = await self.prompt_gateway.execute(model, messages, tools, dispatch)
        self.decisions.append(decision)
        await self.settle()
        return result

    def prompt(self, model, messages, tools, backend):
        return self.runner.run(self._prompt(model, messages, tools, backend))

    async def _finish(self):
        await self.gateway.finish(self.contract.session_id)
        await self.settle()
        return await self.verifier.result(self.contract.session_id)

    def finish(self):
        return self.runner.run(self._finish())

    def events(self):
        return self.runner.run(self.persistence.wire_session(self.contract.session_id))

    def close(self):
        if not self.closed:
            self.runner.run(self.aclose())
            self.runner.close()

    async def aclose(self):
        if not self.closed:
            self.manager.ledger.close()
            await self.store.close()
            self.closed = True
