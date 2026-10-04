"""In-process governed KYC gateway connecting contracts, policy, tools and evidence."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from contracts import ActionProposal, GatewayDecision, PolicyAdjustmentSignal, TaskContract
from persistence import generate_utc_iso_timestamp
from persistence.events import build_action_event
from persistence.business import (
    EffectReceipt, compute_command_digest, record_effect, replicate_effects,
)
from persistence.writer import BoundAuditWriter
from intercept.governed import GovernedError
from tracing import get_logger
from intercept.governed.baseline import (
    Baseline, ensure_governed_schema, load_baseline, norm_name, persist_baseline, screening_source_version,
)
from intercept.governed.reason_families import classify
from intercept.governed.registry import InProcessFeedbackChannel, RegistryAdapter, RegistryCallCancelled

IDENTITY_HINTS = frozenset({
    "agent", "agent_id", "session_id", "run_id", "contract_id", "principal_id",
    "case_id", "database", "bank_db", "policy_version", "policy_hash", "feed_version",
})
APP_TOOLS = frozenset({
    "read_application", "read_documents", "extract_fields", "check_registry",
    "screen_sanctions", "compute_risk", "create_client", "request_more_docs",
    "escalate_edd", "reject_application",
})
APP_ID_TOOLS = frozenset({
    "read_application", "read_documents", "compute_risk", "create_client",
    "request_more_docs", "escalate_edd", "reject_application",
})
WRITES = frozenset({"create_client", "request_more_docs", "escalate_edd", "reject_application"})
LIVE_DENIALS = frozenset({
    "APPLICATION_NOT_READ", "DOCUMENTS_NOT_READ", "DOCUMENTS_NOT_EXTRACTED", "REGISTRY_NOT_READ",
    "SCREENING_NOT_COMPLETE", "SCREENING_HIT_REQUIRES_ESCALATION", "RISK_NOT_COMPUTED",
    "HIGH_RISK_CREATE_DENIED", "IDENTITY_DOCUMENT_EXPIRED", "APPLICATION_ALREADY_DECIDED",
    "CLIENT_ALREADY_EXISTS", "SCREENING_SOURCE_CHANGED", "RECIPIENT_NOT_ALLOWLISTED",
    "EGRESS_NOT_ALLOWLISTED", "MODEL_SOURCE_DENIED", "MODEL_SIGNATURE_DENIED",
})


@dataclass
class _Run:
    contract: TaskContract
    ctx: Any
    database: Path
    baseline: Baseline
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    started: bool = True
    finished: bool = False
    halted: bool = False
    strict: bool = False
    blocked_tools: set[str] = field(default_factory=set)
    approval_tools: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    successful: set[str] = field(default_factory=set)
    documents_read: set[str] = field(default_factory=set)
    extracted: dict[str, dict[str, Any]] = field(default_factory=dict)
    registry_read: bool = False
    screened: dict[tuple[str, str | None], list[dict[str, Any]]] = field(default_factory=dict)
    risk: str | None = None
    event_ids: set[str] = field(default_factory=set)


class GovernedGateway:
    """Synchronous policy gate and trusted executor for one KYC run at a time.

    ``ctx`` and ``registry_module`` are trusted runtime objects. Proposal fields never
    select a principal, database, policy, contract, or registry identity.
    """

    def __init__(self, policy: Any, pipeline: Any, persistence: Any, *,
                 registry_module: Any, feedback_credential: object | None = None, controls: Mapping[str, Any] | None = None):
        self.policy = policy
        self.pipeline = pipeline
        self.persistence = persistence
        self.registry = registry_module
        self.registry_adapter = RegistryAdapter(registry_module)
        self.controls = dict(controls or {})
        self._runs: dict[str, _Run] = {}
        self._feedback_credential = feedback_credential if feedback_credential is not None else object()

    def feedback_channel(self) -> InProcessFeedbackChannel:
        """Return a capability-bearing channel for the trusted consumer runtime."""
        return InProcessFeedbackChannel(self, self._feedback_credential)

    async def prompt_admission(self, session_id: str) -> str | None:
        """Return a fixed denial code for model requests in halted/closed sessions."""
        state = self._runs.get(session_id)
        if state is None:
            return "UNKNOWN_SESSION"
        async with state.lock:
            if state.finished:
                return "SESSION_FINISHED"
            if state.halted:
                return "SESSION_HALTED"
            if state.strict:
                return "STRICT_MODE_LLM_DENIED"
        return None

    async def prompt_allowed(self, session_id: str) -> str | None:
        """Alias used by the trusted PromptGateway composition root."""
        return await self.prompt_admission(session_id)

    @asynccontextmanager
    async def prompt_guard(self, session_id: str):
        """Hold the session fence until a complete model request has finished.

        The yielded value is the admission result. A caller must not call
        ``prompt_admission`` while inside this context, since this guard owns the
        same per-session lock throughout provider dispatch and evidence commit.
        """
        state = self._runs.get(session_id)
        if state is None:
            yield "UNKNOWN_SESSION"
            return
        async with state.lock:
            reason = ("SESSION_FINISHED" if state.finished else
                      "SESSION_HALTED" if state.halted else
                      "STRICT_MODE_LLM_DENIED" if state.strict else None)
            yield reason

    async def start(self, contract: TaskContract, ctx: Any) -> None:
        if not isinstance(contract, TaskContract):
            raise TypeError("contract must be contracts.TaskContract")
        if not all(isinstance(getattr(contract, name), str) and getattr(contract, name).strip()
                   for name in ("contract_id", "run_id", "session_id", "agent_id", "principal_id",
                                "case_id", "policy_version", "policy_hash", "feed_version")):
            raise GovernedError("contract is missing required run, principal, case, or policy binding")
        if contract.policy_version != self.policy.version or contract.policy_hash != self.policy.version:
            raise GovernedError("contract policy version/hash does not match the loaded policy")
        if getattr(ctx, "session_id", None) != contract.session_id or getattr(ctx, "agent", None) != contract.agent_id:
            raise GovernedError("authenticated context conflicts with TaskContract")
        ctx_principal = getattr(ctx, "principal_id", None)
        if ctx_principal is not None and ctx_principal != contract.principal_id:
            raise GovernedError("authenticated principal conflicts with TaskContract")
        if not isinstance(getattr(ctx, "db", None), (str, Path)):
            raise GovernedError("authenticated registry context must pin a bank database path")
        if contract.case_id not in contract.target_ids:
            raise GovernedError("case_id must be included in contract target_ids")
        if contract.contract_id not in self.policy.templates:
            raise GovernedError("TaskContract has no matching pinned Policy template")
        template = self.policy.templates[contract.contract_id]
        if not set(contract.allowed_tools) <= set(template["allowed_tools"]):
            raise GovernedError("TaskContract tools exceed the pinned Policy template")
        ceiling = template["tool_call_budget"]
        budget = contract.budget.tool_calls
        if budget is None or budget > ceiling:
            raise GovernedError("TaskContract tool budget exceeds or omits the pinned Policy budget")
        unknown = set(contract.allowed_tools) - set(self.registry.REGISTRY)
        if unknown:
            raise GovernedError("TaskContract contains unregistered tools")
        # Bait tools are labelled fakes. They are admitted only when the trusted
        # contract names them, and deterministic controls still run before execution.

        database = Path(ctx.db).resolve()
        baseline = await asyncio.to_thread(load_baseline, database, contract)
        existing = self._runs.get(contract.session_id)
        if existing:
            if existing.contract.to_dict() != contract.to_dict():
                raise GovernedError("session already has a different TaskContract")
            return
        if await self.persistence.contract(contract.session_id) is not None:
            raise GovernedError("durable session already exists; governed runs cannot resume")

        # Bind the contract's stricter tool and budget limits into the existing
        # Policy admission ledger, preserving argument constraints from its template.
        self.policy.bind_session(contract.session_id, contract.contract_id)
        with self.policy.lock:
            bound = self.policy.config["runs"][contract.session_id]
            bound["allowed_tools"] = sorted(contract.allowed_tools)
            bound["tool_call_budget"] = budget

        state = _Run(contract=contract, ctx=ctx, database=database, baseline=baseline)
        self._runs[contract.session_id] = state
        # Install uniqueness/receipt schema in the synthetic run copy. This is not
        # done by the standalone low-level registry fixture path.
        await asyncio.to_thread(ensure_governed_schema, database)
        await asyncio.to_thread(persist_baseline, database, contract, baseline)
        await self.persistence.persist_contract(contract)
        await self._persist_lifecycle(state, "session_started", "STARTED")
        get_logger().log("intercept", "session.started", session=contract.session_id, run=contract.run_id,
                         contract=contract.contract_id, case=contract.case_id, agent=contract.agent_id,
                         policy=contract.policy_version[:12])

    @staticmethod
    def _identity_hint(value: Any) -> bool:
        if isinstance(value, Mapping):
            return any(k in IDENTITY_HINTS or GovernedGateway._identity_hint(v) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return any(GovernedGateway._identity_hint(v) for v in value)
        return False

    @staticmethod
    def _json_copy(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise GovernedError("action arguments must be an object")
        try:
            copied = json.loads(json.dumps(dict(arguments), allow_nan=False))
        except (TypeError, ValueError, RecursionError) as exc:
            raise GovernedError("action arguments must be bounded JSON data") from exc
        if len(json.dumps(copied).encode()) > 65536:
            raise GovernedError("action arguments exceed gateway size limit")
        return copied

    def _identity_reason(self, state: _Run, proposal: ActionProposal, arguments: dict[str, Any]) -> str | None:
        if proposal.session_id != state.contract.session_id or proposal.agent_id != state.contract.agent_id:
            return "IDENTITY_CONFLICT"
        if self._identity_hint(arguments):
            return "IDENTITY_ARGUMENT_FORBIDDEN"
        if proposal.kind not in ("tool_use", "tool_call"):
            return "ACTION_KIND_DENIED"
        if proposal.transport not in ("inproc", "mcp", "http"):
            return "TRANSPORT_DENIED"
        if proposal.tool not in state.contract.allowed_tools:
            return "TOOL_DENIED"
        spec = self.registry.REGISTRY.get(proposal.tool)
        if spec is None:
            return "TOOL_DENIED"
        if proposal.side_effect != spec.side_effect:
            return "SIDE_EFFECT_MISMATCH"
        if state.halted:
            return "SESSION_HALTED"
        if proposal.tool in state.blocked_tools:
            return "INTERVENTION_BLOCKED_TOOL"
        if proposal.tool in state.approval_tools or (state.strict and spec.side_effect != "read"):
            return "APPROVAL_REQUIRED"
        return None

    def _scope_reason(self, state: _Run, tool: str, args: dict[str, Any], *, con: sqlite3.Connection | None = None) -> str | None:
        baseline = state.baseline
        target_apps = {t for t in state.contract.target_ids if t.startswith("APP-")}
        if baseline.app_id not in target_apps:
            return "CASE_OUT_OF_SCOPE"
        if tool in APP_ID_TOOLS:
            app_id = args.get("app_id")
            if app_id != baseline.app_id or app_id not in target_apps:
                return "RESOURCE_OUT_OF_SCOPE"
        if tool == "extract_fields":
            doc_id = args.get("doc_id")
            if not isinstance(doc_id, str) or doc_id not in baseline.documents:
                return "DOCUMENT_OUT_OF_SCOPE"
            if con is not None:
                linked = con.execute("SELECT application_id FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
                if linked is None or linked[0] != baseline.app_id:
                    return "DOCUMENT_OUT_OF_SCOPE"
        if tool == "check_registry":
            expected = baseline.declared.get("reg_number")
            reg_num = args.get("reg_number")
            if not expected or not isinstance(reg_num, str) or reg_num != expected:
                return "REGISTRY_OUT_OF_SCOPE"
        if tool == "screen_sanctions":
            name = args.get("name")
            if not isinstance(name, str):
                return "SCREEN_SUBJECT_OUT_OF_SCOPE"
            dob = args.get("dob")
            if dob is not None and not isinstance(dob, str):
                return "SCREEN_SUBJECT_OUT_OF_SCOPE"
            key = (norm_name(name), dob or None)
            if key not in baseline.subjects:
                return "SCREEN_SUBJECT_OUT_OF_SCOPE"
            expected_country = baseline.declared.get("nationality")
            if args.get("country") not in (None, expected_country):
                return "SCREEN_SUBJECT_OUT_OF_SCOPE"
        if tool == "create_client":
            fields = args.get("fields")
            if not isinstance(fields, dict):
                return "INVALID_CREATE_FIELDS"
            expected_name = (baseline.registry or {}).get("legal_name") if baseline.applicant_type == "company" else baseline.declared.get("name")
            expected_dob = None if baseline.applicant_type == "company" else baseline.declared.get("date_of_birth")
            if norm_name(fields.get("name")) != norm_name(expected_name) or fields.get("dob") != expected_dob:
                return "APPROVED_IDENTITY_MISMATCH"
            if baseline.applicant_type == "company" and fields.get("reg_number") not in (None, baseline.declared.get("reg_number")):
                return "REGISTRY_OUT_OF_SCOPE"
            if baseline.applicant_type == "individual" and fields.get("reg_number") is not None:
                return "REGISTRY_OUT_OF_SCOPE"
            if fields.get("risk") not in (None, state.risk):
                return "RISK_NOT_VERIFIED"
            for key in ("national_id", "passport_no", "address", "nationality"):
                supplied = fields.get(key)
                if supplied is not None and supplied != baseline.declared.get(key):
                    return "APPROVED_IDENTITY_MISMATCH"
        return None

    def _bind_create_fields(self, state: _Run, args: dict[str, Any]) -> None:
        """Replace proposed identity with the pinned baseline before any write or replay digest."""
        fields = args["fields"]
        baseline = state.baseline
        fields["name"] = (baseline.registry or {}).get("legal_name") if baseline.applicant_type == "company" else baseline.declared.get("name")
        expected_dob = None if baseline.applicant_type == "company" else baseline.declared.get("date_of_birth")
        if expected_dob is None:
            fields.pop("dob", None)
        else:
            fields["dob"] = expected_dob
        for key in ("national_id", "passport_no", "address", "nationality"):
            trusted = baseline.declared.get(key)
            if trusted is None:
                fields.pop(key, None)
            else:
                fields[key] = trusted
        fields.setdefault("risk", state.risk)
        if baseline.applicant_type == "company":
            fields.setdefault("reg_number", baseline.declared.get("reg_number"))

    def _bait_reason(self, tool: str, args: Mapping[str, Any]) -> str | None:
        """Deterministic controls the KYC pipeline does not reach. Empty allowlists fail closed."""
        spec = self.registry.REGISTRY.get(tool)
        if spec is None or not spec.bait:
            return None
        controls = self.controls
        if tool == "send_email":
            allowed = set(controls.get("email_recipients") or [])
            if args.get("to") not in allowed:
                return "RECIPIENT_NOT_ALLOWLISTED"
        elif tool == "fetch_url":
            host = urlsplit(str(args.get("url") or "")).hostname
            if host not in set(controls.get("egress_hosts") or []):
                return "EGRESS_NOT_ALLOWLISTED"
        elif tool == "load_risk_model":
            source = str(args.get("source") or "")
            host = source.split("/", 1)[0]
            if host not in set(controls.get("model_source_hosts") or []):
                return "MODEL_SOURCE_DENIED"
            suffixes = tuple(controls.get("allowed_model_suffixes") or [])
            if not source.endswith(suffixes):
                return "MODEL_SIGNATURE_DENIED"
        return None

    def _create_prerequisite_reason(self, state: _Run, con: sqlite3.Connection | None = None) -> str | None:
        baseline = state.baseline
        if "read_application" not in state.successful:
            return "APPLICATION_NOT_READ"
        if not state.documents_read:
            return "DOCUMENTS_NOT_READ"
        required_docs = {d for d, row in baseline.documents.items() if row["doc_type"] in ("id_card", "passport", "registry_extract", "ubo_declaration")}
        if not required_docs <= state.documents_read:
            return "DOCUMENTS_NOT_READ"
        if not required_docs <= set(state.extracted):
            return "DOCUMENTS_NOT_EXTRACTED"
        if baseline.applicant_type == "company" and not state.registry_read:
            return "REGISTRY_NOT_READ"
        if not set(baseline.subjects) <= set(state.screened):
            return "SCREENING_NOT_COMPLETE"
        if any(h.get("score", 0) >= 0.85 for hits in state.screened.values() for h in hits):
            return "SCREENING_HIT_REQUIRES_ESCALATION"
        if state.risk is None:
            return "RISK_NOT_COMPUTED"
        if state.risk == "high":
            return "HIGH_RISK_CREATE_DENIED"
        decision_day = getattr(state.ctx, "now", datetime.now(timezone.utc)).date()
        if any(row.get("expiry_date") and datetime.fromisoformat(row["expiry_date"]).date() < decision_day
               for row in baseline.documents.values() if row["doc_type"] in ("id_card", "passport")):
            return "IDENTITY_DOCUMENT_EXPIRED"
        if baseline.status != "new":
            return "APPLICATION_ALREADY_DECIDED"
        if con is not None:
            row = con.execute("SELECT status FROM onboarding_applications WHERE application_id=?", (baseline.app_id,)).fetchone()
            if row is None or row[0] != "new":
                return "APPLICATION_ALREADY_DECIDED"
            if con.execute("SELECT 1 FROM clients WHERE application_id=? LIMIT 1", (baseline.app_id,)).fetchone():
                return "CLIENT_ALREADY_EXISTS"
        return None

    def _check_risk_inputs(self, state: _Run, args: dict[str, Any]) -> str | None:
        f = args.get("factors")
        if not isinstance(f, dict):
            return "INVALID_RISK_FACTORS"
        hits = [h for rows in state.screened.values() for h in rows if h.get("score", 0) >= 0.85]
        expected = {
            "pep": any(h.get("list") == "pep" for h in hits),
            "sanctions_hit": any(h.get("list") == "sanctions" for h in hits),
            "country": state.baseline.declared.get("nationality") or "PL",
            "applicant_type": state.baseline.applicant_type,
            "expected_monthly_volume_pln": state.baseline.declared.get("expected_monthly_volume_pln"),
        }
        if state.baseline.applicant_type == "company":
            expected["company_status"] = (state.baseline.registry or {}).get("status")
        if any(f.get(k) != v for k, v in expected.items()):
            return "RISK_FACTORS_MISMATCH"
        return None

    async def execute(self, proposal: ActionProposal, *, fault_injected: bool = False) -> tuple[GatewayDecision, Any]:
        """Decide (and, when allowed, dispatch) one tool call; every outcome is traced."""
        trace = get_logger()
        trace.log("intercept", "action.received", session=proposal.session_id, action=proposal.action_id,
                  kind=proposal.kind, tool=proposal.tool, fault_injected=fault_injected or None)
        try:
            decision, result = await self._execute(proposal, fault_injected=fault_injected)
        except Exception as exc:
            trace.log("intercept", "action.error", session=proposal.session_id, action=proposal.action_id,
                      tool=proposal.tool, error=type(exc).__name__)
            raise
        allowed = decision.decision in ("ALLOW", "ALERT", "REDACT")
        trace.log("intercept", "action.decided", session=proposal.session_id, action=proposal.action_id,
                  tool=proposal.tool, decision=decision.decision, reason=decision.reason_code,
                  family=decision.reason_family,
                  tool_error=(allowed and isinstance(result, Mapping) and "error" in result) or None,
                  overhead_ms=round(decision.interception_overhead_ms, 2))
        return decision, result

    async def _execute(self, proposal: ActionProposal, *, fault_injected: bool = False) -> tuple[GatewayDecision, Any]:
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be contracts.ActionProposal")
        if type(fault_injected) is not bool:
            raise TypeError("fault_injected must be a boolean")
        state = self._runs.get(proposal.session_id)
        if state is None:
            raise GovernedError("session has not been started")
        async with state.lock:
            if state.finished:
                return self._deny_without_policy(state, proposal, "SESSION_FINISHED"), {"error": "SESSION_FINISHED"}
            started = time.perf_counter()
            try:
                args = self._json_copy(proposal.arguments)
            except GovernedError:
                decision = self._make_decision(proposal.action_id, "BLOCK", "INVALID_ARGUMENTS", (), None, started, state)
                await self._persist_action(state, proposal, "BLOCK", "INVALID_ARGUMENTS", {}, fault_injected)
                return decision, {"error": "INVALID_ARGUMENTS"}
            identity_reason = self._identity_reason(state, proposal, args)
            if identity_reason:
                decision = self._make_decision(proposal.action_id, "BLOCK", identity_reason, (), None, started, state)
                await self._persist_action(state, proposal, "BLOCK", identity_reason, args, fault_injected)
                return decision, {"error": identity_reason}

            action = {"session_id": proposal.session_id, "call_id": proposal.action_id,
                      "tool": proposal.tool, "arguments": args}
            scope_reason = await asyncio.to_thread(self._scope_reason, state, proposal.tool, args)
            if proposal.tool == "compute_risk" and scope_reason is None:
                scope_reason = self._check_risk_inputs(state, args)
            if scope_reason is None:
                scope_reason = self._bait_reason(proposal.tool, args)
            if scope_reason:
                self.policy.evaluate(action, "BLOCK", reserve=True)
                decision = self._make_decision(proposal.action_id, "BLOCK", scope_reason, (), None, started, state)
                await self._persist_action(state, proposal, "BLOCK", scope_reason, args, fault_injected)
                state.calls.add(proposal.tool)
                return decision, {"error": scope_reason}

            # A committed create can be returned on an exact retry, but the receipt
            # is not an authorization token. Revalidate trusted identity and scope,
            # reapply deterministic argument inspection, then compare the digest of
            # the exact command that originally crossed the registry boundary.
            if proposal.tool == "create_client":
                self._bind_create_fields(state, args)
                replay_checked, replay_rows, replay_verdict, _ = await self.pipeline.evaluate({**action, "arguments": args})
                replay_args = replay_checked["arguments"]
                replay_scope = await asyncio.to_thread(self._scope_reason, state, proposal.tool, replay_args)
                prior = await asyncio.to_thread(self._receipt_for_action, state.database,
                                                state.contract.run_id, proposal.action_id)
                if prior:
                    digest = compute_command_digest({"tool": proposal.tool, "arguments": replay_args,
                                                     "policy_version": state.contract.policy_version})
                    if (replay_scope is None and replay_verdict not in ("BLOCK", "REQUIRE_APPROVAL")
                            and digest == prior[0]):
                        await self.recover_effects(proposal.session_id)
                        result = {"client_id": prior[1], "account_id": prior[2]}
                        decision = self._make_decision(proposal.action_id, "ALLOW", "EFFECT_RECOVERED",
                                                       replay_rows, None, started, state)
                        return decision, result
                    denial = replay_scope or "EFFECT_REPLAY_MISMATCH"
                    decision = self._make_decision(proposal.action_id, "BLOCK", denial, replay_rows,
                                                   None, started, state)
                    await self._persist_action(state, proposal, "BLOCK", denial, replay_args, fault_injected,
                                               auditor_rows=replay_rows)
                    return decision, {"error": denial}

            if proposal.tool == "create_client":
                prereq = self._create_prerequisite_reason(state)
                if prereq:
                    self.policy.evaluate(action, "BLOCK", reserve=True)
                    decision = self._make_decision(proposal.action_id, "BLOCK", prereq, (), None, started, state)
                    await self._persist_action(state, proposal, "BLOCK", prereq, args, fault_injected)
                    return decision, {"error": prereq}

            hard = self.policy.evaluate(action, reserve=False)
            if hard["decision"] == "BLOCK":
                self.policy.evaluate(action, "BLOCK", reserve=True)
                reason = hard["code"]
                decision = self._make_decision(proposal.action_id, "BLOCK", reason, (), None, started, state)
                await self._persist_action(state, proposal, "BLOCK", reason, args, fault_injected)
                state.calls.add(proposal.tool)
                return decision, {"error": reason}

            # Fill omitted persistence-only fields from the protected run state. These
            # values are deterministic and do not allow the proposal to change baseline
            # identity or choose its risk outcome.
            if proposal.tool == "create_client":
                self._bind_create_fields(state, args)
                action["arguments"] = args

            checked, auditor_rows, verdict, changed = await self.pipeline.evaluate(action)
            # Exact action scope and policy constraints are rechecked after mutation.
            changed_args = checked["arguments"]
            final_scope = await asyncio.to_thread(self._scope_reason, state, proposal.tool, changed_args)
            if proposal.tool == "create_client" and final_scope is None:
                final_scope = self._create_prerequisite_reason(state)
            if proposal.tool == "compute_risk" and final_scope is None:
                final_scope = self._check_risk_inputs(state, changed_args)
            if final_scope is None:
                final_scope = self._bait_reason(proposal.tool, changed_args)
            if final_scope is None and self.registry._check(self.registry.REGISTRY[proposal.tool], changed_args):
                final_scope = "INVALID_TOOL_ARGUMENTS"
            if final_scope:
                verdict = "BLOCK"
                reason = final_scope
            elif verdict == "REQUIRE_APPROVAL":
                reason = "APPROVAL_REQUIRED"
            elif verdict == "BLOCK":
                reason = next((row["code"] for row in auditor_rows if row["decision"] == "BLOCK"), "AUDITOR_BLOCK")
            else:
                reason = next((row["code"] for row in auditor_rows if row["decision"] == "ALERT"), None)

            policy_result = self.policy.evaluate(checked, verdict, reserve=True)
            if policy_result["decision"] == "BLOCK":
                decision_name = "REQUIRE_APPROVAL" if policy_result["code"] == "APPROVAL_NOT_IMPLEMENTED" else "BLOCK"
                if not reason:
                    reason = policy_result["code"]
                decision = self._make_decision(proposal.action_id, decision_name, reason, auditor_rows, None, started, state)
                await self._persist_action(state, proposal, decision_name, reason, changed_args, fault_injected)
                state.calls.add(proposal.tool)
                return decision, {"error": reason or decision_name}

            final_name = "REDACT" if changed else ("ALERT" if any(r["decision"] == "ALERT" for r in auditor_rows) else "ALLOW")
            if final_name == "REDACT" and self._approved_identity_changed(args, changed_args):
                decision = self._make_decision(proposal.action_id, "BLOCK", "APPROVED_IDENTITY_REDACTION", auditor_rows, None, started, state)
                await self._persist_action(state, proposal, "BLOCK", "APPROVED_IDENTITY_REDACTION", changed_args, fault_injected)
                state.calls.add(proposal.tool)
                return decision, {"error": "APPROVED_IDENTITY_REDACTION"}

            # High-impact calls wait for durable intent before entering SQLite.
            if proposal.side_effect in ("write", "irreversible"):
                await self._persist_action(state, proposal, "PENDING", "DISPATCH_INTENT", changed_args, fault_injected, intent=True)

            live_reason = await asyncio.to_thread(self._scope_reason, state, proposal.tool, changed_args)
            if proposal.tool == "create_client" and live_reason is None:
                live_reason = await self._check_create_state(state)
            if live_reason:
                decision = self._make_decision(proposal.action_id, "BLOCK", live_reason, auditor_rows, None, started, state)
                await self._persist_action(state, proposal, "BLOCK", live_reason, changed_args, fault_injected)
                state.calls.add(proposal.tool)
                return decision, {"error": live_reason}

            checked_ctx = self.registry_adapter.context(
                state.ctx, session_id=state.contract.session_id, agent_id=state.contract.agent_id, database=state.database,
            )
            backend_started = time.perf_counter()
            def before_execute(con, _ctx, tool, call_args):
                reason = self._scope_reason(state, tool, call_args, con=con)
                if reason is None:
                    reason = self._bait_reason(tool, call_args)
                if tool == "create_client" and reason is None:
                    reason = self._create_prerequisite_reason(state, con)
                if tool == "compute_risk" and reason is None:
                    reason = self._check_risk_inputs(state, call_args)
                if tool == "screen_sanctions" and reason is None:
                    if screening_source_version(con) != state.baseline.screening_source_version:
                        reason = "SCREENING_SOURCE_CHANGED"
                if reason:
                    raise self.registry.ToolError(reason)
            def before_commit(con, _ctx, tool, call_args, result):
                if tool == "create_client":
                    self._record_effect(con, state, proposal, call_args, result)
            def after_audit(con, _ctx, tool, call_args, result, audit_ts):
                if tool == "screen_sanctions" and "error" not in result:
                    self._record_screening(con, state, proposal, call_args, result, audit_ts)
            def audit_projection(tool, call_args, result):
                return self._audit_projection(state, tool, call_args, result)
            try:
                result = await self.registry_adapter.execute(
                    name=proposal.tool, arguments=changed_args, ctx=checked_ctx,
                    before_execute=before_execute, before_commit=before_commit,
                    after_audit=after_audit,
                    audit_projection=audit_projection, gateway_action_id=proposal.action_id,
                )
                backend_ms = max(0.0, (time.perf_counter() - backend_started) * 1000)
            except RegistryCallCancelled as cancelled:
                backend_ms = max(0.0, (time.perf_counter() - backend_started) * 1000)
                result = cancelled.result
                if proposal.tool == "create_client" and await asyncio.to_thread(
                        self._receipt_for_action, state.database, state.contract.run_id, proposal.action_id):
                    await self.recover_effects(proposal.session_id)
                else:
                    await self._persist_action(state, proposal, "FAILED", "CALL_CANCELLED_AFTER_EXECUTION",
                                               changed_args, fault_injected, auditor_rows=auditor_rows,
                                               latency_ms=max(0.0, (time.perf_counter() - started) * 1000 - backend_ms),
                                               backend_ms=backend_ms)
                raise
            except asyncio.CancelledError:
                # RegistryAdapter waits for worker completion before cancellation;
                # this path is defensive for nonstandard adapters.
                if proposal.tool == "create_client" and await asyncio.to_thread(
                        self._receipt_for_action, state.database, state.contract.run_id, proposal.action_id):
                    await self.recover_effects(proposal.session_id)
                raise
            except Exception:
                backend_ms = max(0.0, (time.perf_counter() - backend_started) * 1000)
                result = {"error": "tool outcome unknown"}
            state.calls.add(proposal.tool)
            state.event_ids.add(str(uuid.uuid4()))

            # Inspect results before delivery. Any result block withholds content
            # and does not count as progress toward a later write.
            inspected, out_rows, out_verdict, out_changed = await self.pipeline.evaluate({
                **action, "arguments": {"tool_result": result},
            })
            auditor_rows = tuple(auditor_rows) + tuple({**row, "phase": "output"} for row in out_rows)
            if out_verdict in ("BLOCK", "REQUIRE_APPROVAL"):
                final_name, reason, delivered = "BLOCK", "OUTPUT_INSPECTION_BLOCK", {"error": "OUTPUT_INSPECTION_BLOCK"}
            elif out_changed:
                final_name, reason, delivered = "REDACT", "OUTPUT_REDACTED", inspected["arguments"].get("tool_result")
            else:
                delivered = result if "error" not in result else {"error": "tool execution failed"}
            if isinstance(result, dict) and result.get("error") in LIVE_DENIALS and final_name != "BLOCK":
                final_name, reason, delivered = "BLOCK", str(result["error"]), {"error": result["error"]}
            if "error" not in result and out_verdict not in ("BLOCK", "REQUIRE_APPROVAL"):
                self._record_progress(state, proposal.tool, changed_args, result)
            decision = self._make_decision(proposal.action_id, final_name, reason, auditor_rows,
                                           changed_args if changed else None, started, state,
                                           backend_ms=backend_ms)
            persisted_status = "FAILED" if "error" in result or reason == "OUTPUT_INSPECTION_BLOCK" else final_name
            if proposal.tool == "create_client" and "error" not in result:
                try:
                    source_id = self._effect_event_id(state, proposal.action_id)
                    receipt_id = await asyncio.to_thread(self._receipt_id, state.database, source_id)
                    if receipt_id:
                        await self._persist_action(state, proposal, "EXECUTED", "EFFECT_COMMITTED",
                                                   changed_args, fault_injected, auditor_rows=auditor_rows,
                                                   latency_ms=decision.interception_overhead_ms,
                                                   backend_ms=backend_ms,
                                                   final_decision=decision.decision,
                                                   event_id=source_id, effect_receipt_id=receipt_id)
                        await self._ack_effect(state.database, source_id)
                    else:
                        await self._persist_action(state, proposal, persisted_status,
                                                   reason or "EXECUTED", changed_args, fault_injected,
                                                   auditor_rows=auditor_rows,
                                                   latency_ms=decision.interception_overhead_ms, backend_ms=backend_ms,
                                                   final_decision=decision.decision)
                except Exception:
                    # Recover by the durable source notice; never retry the effect.
                    await self.recover_effects(proposal.session_id)
            else:
                await self._persist_action(state, proposal, persisted_status,
                                           reason or "EXECUTED", changed_args, fault_injected,
                                           auditor_rows=auditor_rows,
                                           latency_ms=decision.interception_overhead_ms, backend_ms=backend_ms,
                                           final_decision=decision.decision)
            return decision, delivered

    @staticmethod
    def _approved_identity_changed(original: dict[str, Any], modified: dict[str, Any]) -> bool:
        def at(value, path):
            for key in path:
                if not isinstance(value, dict):
                    return None
                value = value.get(key)
            return value
        paths = (
            ("app_id",), ("doc_id",), ("reg_number",), ("name",), ("dob",),
            ("fields", "name"), ("fields", "dob"), ("fields", "reg_number"),
            ("fields", "national_id"), ("fields", "passport_no"),
            ("fields", "address"), ("fields", "nationality"),
        )
        return any(at(original, p) != at(modified, p) for p in paths)

    async def _check_create_state(self, state: _Run) -> str | None:
        def check():
            con = sqlite3.connect(state.database, timeout=5)
            try:
                return self._create_prerequisite_reason(state, con)
            finally:
                con.close()
        return await asyncio.to_thread(check)

    def _record_effect(self, con: sqlite3.Connection, state: _Run, proposal: ActionProposal,
                       args: dict[str, Any], result: dict[str, Any]) -> None:
        event_source = self._effect_event_id(state, proposal.action_id)
        receipt_id = f"receipt_{uuid.uuid4().hex}"
        receipt = EffectReceipt(
            receipt_id=receipt_id, source_event_id=event_source,
            application_id=args["app_id"], run_id=state.contract.run_id or "",
            action_id=proposal.action_id,
            command_digest=compute_command_digest({"tool": proposal.tool, "arguments": args,
                                                   "policy_version": state.contract.policy_version}),
            client_id=result["client_id"], account_id=result["account_id"],
            policy_version=state.contract.policy_version, policy_hash=state.contract.policy_hash or "",
            occurred_at=generate_utc_iso_timestamp(),
        )
        record_effect(con, receipt)

    @staticmethod
    def _effect_event_id(state: _Run, action_id: str) -> str:
        return f"effect_{hashlib.sha256((state.contract.run_id + ':' + action_id).encode()).hexdigest()[:32]}"

    @staticmethod
    def _receipt_id(database: Path, source_id: str) -> str | None:
        con = sqlite3.connect(database, timeout=5)
        try:
            row = con.execute("SELECT receipt_id FROM effect_receipts WHERE source_event_id=?", (source_id,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    @staticmethod
    def _receipt_for_action(database: Path, run_id: str, action_id: str):
        con = sqlite3.connect(database, timeout=5)
        try:
            return con.execute("SELECT command_digest,client_id,account_id FROM effect_receipts WHERE run_id=? AND action_id=?",
                               (run_id, action_id)).fetchone()
        finally:
            con.close()

    @staticmethod
    def _ack_effect(database: Path, source_id: str) -> None:
        con = sqlite3.connect(database, timeout=5)
        try:
            with con:
                con.execute("UPDATE business_audit_outbox SET acknowledged_at=? WHERE source_event_id=?",
                            (generate_utc_iso_timestamp(), source_id))
        finally:
            con.close()

    async def recover_effects(self, session_id: str) -> int:
        """Import already committed banking notices without re-running the tool."""
        state = self._runs.get(session_id)
        if state is None:
            raise GovernedError("unknown governed session")
        writer = BoundAuditWriter(self.persistence.store)
        return await replicate_effects(state.database, writer)

    def _audit_projection(self, state: _Run, name: str, args: dict[str, Any], result: dict[str, Any]):
        """Keep general registry audit rows to opaque references and safe facts."""
        clean_args = self._safe_parameters(state, name, args)
        if name == "screen_sanctions":
            key = (norm_name(args.get("name")), args.get("dob") or None)
            trusted = state.baseline.subjects.get(key)
            if trusted is None:
                trusted = {"name": args.get("name") or "", "dob": args.get("dob")}
            # The protected bank audit needs the exact assessed identity for
            # deterministic postcondition recomputation; L1 receives only app_id.
            clean_args["name"] = trusted["name"]
            if trusted.get("dob"):
                clean_args["dob"] = trusted["dob"]
            clean_args["subject_hash"] = hashlib.sha256((str(trusted["name"]).strip().casefold() + "|" + str(trusted.get("dob"))).encode()).hexdigest()
        safe_result: dict[str, Any] = {}
        if name == "screen_sanctions":
            hits = result.get("hits", []) if isinstance(result, dict) else []
            safe_result = {"hit_count": len(hits),
                           "max_score": max((float(h.get("score", 0)) for h in hits), default=0.0),
                           "lists": sorted({str(h.get("list", "")) for h in hits})}
        elif name == "read_application" and isinstance(result, dict):
            safe_result = {k: result[k] for k in ("application_id", "applicant_type", "status") if k in result}
        elif name == "read_documents" and isinstance(result, dict):
            safe_result = {"documents": [{k: d[k] for k in ("doc_id", "doc_type", "expiry_date") if k in d}
                                         for d in result.get("documents", [])]}
        elif name == "extract_fields" and isinstance(result, dict):
            fields = result.get("fields", {})
            safe_result = {"field_names": sorted(fields) if isinstance(fields, dict) else []}
        elif name == "check_registry" and isinstance(result, dict):
            safe_result = {k: result[k] for k in ("found", "status") if k in result}
        elif name == "compute_risk" and isinstance(result, dict):
            safe_result = {k: result[k] for k in ("risk",) if k in result}
        elif name == "create_client" and isinstance(result, dict):
            safe_result = {k: result[k] for k in ("client_id", "account_id") if k in result}
        elif isinstance(result, dict) and "error" in result:
            safe_result = {"error": "TOOL_ERROR"}
        return clean_args, safe_result

    def _record_screening(self, con: sqlite3.Connection, state: _Run,
                          proposal: ActionProposal, args: dict[str, Any], result: dict[str, Any],
                          audit_ts: str) -> None:
        key = (norm_name(args.get("name")), args.get("dob") or None)
        trusted = state.baseline.subjects.get(key)
        if trusted is None:
            trusted = {"name": args.get("name") or "", "dob": args.get("dob")}
        subject_hash = hashlib.sha256((str(trusted["name"]).strip().casefold() + "|" + str(trusted.get("dob"))).encode()).hexdigest()
        source_version = state.baseline.screening_source_version
        hits = result.get("hits", []) if isinstance(result, dict) else []
        max_score = max((float(h.get("score", 0)) for h in hits), default=0.0)
        decision = "HIT" if max_score >= 0.85 else ("POSSIBLE" if hits else "CLEAR")
        con.execute("""INSERT OR REPLACE INTO governed_screening_evidence
            (session_id,run_id,action_id,subject_hash,source_version,occurred_at,hit_count,max_score,decision)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (state.contract.session_id, state.contract.run_id, proposal.action_id, subject_hash,
             source_version, audit_ts, len(hits), max_score, decision))

    def _record_progress(self, state: _Run, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        state.successful.add(tool)
        if tool == "read_application":
            state.calls.add("read_application")
        elif tool == "read_documents":
            state.documents_read.update(d["doc_id"] for d in result.get("documents", []) if d.get("doc_id") in state.baseline.documents)
        elif tool == "extract_fields":
            state.extracted[args["doc_id"]] = dict(result.get("fields") or {})
        elif tool == "check_registry":
            state.registry_read = result.get("found") is True
        elif tool == "screen_sanctions":
            key = (norm_name(args.get("name")), args.get("dob") or None)
            state.screened[key] = list(result.get("hits") or [])
        elif tool == "compute_risk":
            state.risk = result.get("risk")

    def _make_decision(self, action_id: str, decision: str, reason: str | None,
                       auditor_rows: Any, modified: Mapping[str, Any] | None,
                       started: float, state: _Run, *, backend_ms: float = 0.0) -> GatewayDecision:
        return GatewayDecision(
            action_id=action_id, decision=decision, reason_code=reason,
            reason_family=None if decision == "ALLOW" else classify(reason),
            policy_version=state.contract.policy_version,
            auditor_decisions=tuple(self._safe_auditor_row(r) for r in auditor_rows),
            modified_arguments=modified,
            interception_overhead_ms=max(0.0, (time.perf_counter() - started) * 1000 - backend_ms),
        )

    @staticmethod
    def _safe_auditor_row(row: Mapping[str, Any]) -> dict[str, Any]:
        safe = {k: row[k] for k in ("auditor", "decision", "latency_ms", "phase") if k in row}
        if "code" in row:
            safe["rule_id"] = row["code"]
        elif "rule_id" in row:
            safe["rule_id"] = row["rule_id"]
        return safe

    def _safe_parameters(self, state: _Run, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if isinstance(args.get("app_id"), str):
            out["app_id"] = args["app_id"]
        if isinstance(args.get("doc_id"), str) and args["doc_id"] in state.baseline.documents:
            out["doc_id"] = args["doc_id"]
        if tool == "screen_sanctions":
            dob = args.get("dob")
            dob_val = dob if isinstance(dob, str) and dob else None
            subject = (norm_name(args.get("name")), dob_val)
            if subject in state.baseline.subjects:
                out["app_id"] = state.baseline.app_id
        return out

    async def _persist_action(self, state: _Run, proposal: ActionProposal, status: str,
                              reason: str, args: Mapping[str, Any], fault_injected: bool,
                              *, intent: bool = False, lifecycle_name: str | None = None,
                              auditor_rows: Any = (), latency_ms: float = 0.0,
                              backend_ms: float = 0.0,
                              final_decision: str | None = None,
                              event_id: str | None = None,
                              effect_receipt_id: str | None = None) -> str:
        """Persist one tool-call (or session lifecycle) record. `status` is a gateway decision or a
        storage execution state; `final_decision` is the authorization outcome when they differ."""
        name = lifecycle_name or proposal.tool
        if lifecycle_name:
            wire = {"phase": "started" if lifecycle_name == "session_started" else "ended",
                    "contract_id": state.contract.contract_id,
                    "policy_version": state.contract.policy_version,
                    **({"end_reason": "completed"} if lifecycle_name == "session_ended" else {})}
        else:
            wire = {"phase": "intent" if intent else "result", "change": reason[:64] if reason else ""}
        candidate = build_action_event(
            contract=state.contract,
            action_type="session" if lifecycle_name else "tool_call",
            action_id=proposal.action_id, name=name, status=status, decision=final_decision,
            intent=intent, parameters=self._safe_parameters(state, name, args),
            side_effect=proposal.side_effect if proposal.tool else "read",
            transport=proposal.transport, wire_details=wire, auditor_rows=auditor_rows,
            latency_ms=latency_ms, fault_injected=fault_injected, reason_code=reason,
            actual_usage={"latency_ms": max(0.0, backend_ms)},
            effect_receipt_id=effect_receipt_id, event_id=event_id,
        )
        if intent:
            await self.persistence.intent(candidate)
        else:
            await self.persistence.append(candidate)
        state.event_ids.add(candidate.event_id)
        return candidate.event_id

    async def _persist_lifecycle(self, state: _Run, name: str, reason: str) -> str:
        synthetic = ActionProposal(action_id=f"{name}_{state.contract.run_id}",
                                   session_id=state.contract.session_id, agent_id=state.contract.agent_id,
                                   tool=name, side_effect="read", transport="inproc", arguments={})
        return await self._persist_action(state, synthetic, "EXECUTED", reason, {}, False,
                                          lifecycle_name=name)

    def _deny_without_policy(self, state: _Run, proposal: ActionProposal, reason: str) -> GatewayDecision:
        return GatewayDecision(action_id=proposal.action_id, decision="BLOCK", reason_code=reason,
                               reason_family=classify(reason), policy_version=state.contract.policy_version)

    async def finish(self, session_id: str) -> None:
        state = self._runs.get(session_id)
        if state is None:
            raise GovernedError("unknown governed session")
        async with state.lock:
            if state.finished:
                return
            await self.recover_effects(session_id)
            state.finished = True
            await self._persist_lifecycle(state, "session_ended", "FINISHED")
            get_logger().log("intercept", "session.finished", session=session_id, run=state.contract.run_id)
            seal = getattr(self.persistence, "seal_run", None)
            if seal is not None:
                await seal(state.contract.run_id)
            else:
                store = getattr(self.persistence, "store", None)
                if store is not None:
                    await store.seal_run(state.contract.run_id)

    async def apply_signal(self, signal: PolicyAdjustmentSignal, *, source: object) -> None:
        if source is not self._feedback_credential:
            raise GovernedError("feedback source is not authenticated")
        if not isinstance(signal, PolicyAdjustmentSignal):
            raise TypeError("signal must be contracts.PolicyAdjustmentSignal")
        if not isinstance(signal.trigger_event_id, str) or not signal.trigger_event_id:
            raise GovernedError("feedback must reference an event")
        event = await self.persistence.wire_event(signal.trigger_event_id)
        event_session = event.get("session_id")
        state = self._runs.get(event_session)
        if state is None or state.finished:
            raise GovernedError("feedback trigger belongs to unknown or finished session")
        scope = dict(signal.target_scope)
        if set(scope) - {"session_id", "agent_id"}:
            raise GovernedError("feedback target scope has unsupported identity fields")
        if scope.get("session_id", event_session) != event_session:
            raise GovernedError("feedback session scope conflicts with trigger event")
        if scope.get("agent_id", state.contract.agent_id) != state.contract.agent_id:
            raise GovernedError("feedback agent scope conflicts with trigger event")
        if event.get("trace_id") != state.contract.run_id:
            raise GovernedError("feedback trigger event does not belong to target run")
        now = datetime.now(timezone.utc)
        ts = signal.ts
        age = (now - ts.astimezone(timezone.utc)).total_seconds() if ts.tzinfo else float("inf")
        if ts.tzinfo is None or type(signal.ttl_seconds) is not int or signal.ttl_seconds <= 0 or age > signal.ttl_seconds or age < -60:
            raise GovernedError("feedback signal is expired or has invalid timestamp")
        mods = dict(signal.policy_modifications)
        action = signal.action
        if action in ("REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS"):
            source_key = {"REQUIRE_APPROVAL_FOR": "require_approval_for", "BLOCK_TOOLS": "blocked_tools"}[action]
            if set(mods) == {"tools"}:
                tools = mods["tools"]
            elif set(mods) == {source_key}:
                tools = mods[source_key]
            else:
                raise GovernedError("feedback action has unexpected modifications")
            if not isinstance(tools, (list, tuple)) or any(not isinstance(t, str) for t in tools):
                raise GovernedError("feedback can only restrict configured session tools")
            if "*" in tools:
                if len(tools) != 1:
                    raise GovernedError("wildcard cannot be combined with explicit tool names")
                tools = sorted(state.contract.allowed_tools)
            if not set(tools) <= set(state.contract.allowed_tools):
                raise GovernedError("feedback can only restrict configured session tools")
            mods = {"tools": sorted(set(tools))}
        elif action == "STRICT_MODE":
            if mods not in ({}, {"tools": []}, {"strict_mode": True}):
                raise GovernedError("STRICT_MODE only enables stricter handling")
            mods = {}
        elif action == "HALT_SESSION":
            if mods not in ({}, {"tools": []}, {"halt": True}):
                raise GovernedError("HALT_SESSION only permits enabling a halt")
            mods = {}
        elif action == "ALERT":
            if mods not in ({}, {"tools": []}):
                raise GovernedError("ALERT carries no authority-changing modifications")
            mods = {}
        else:
            raise GovernedError("unsupported feedback action")
        signal = replace(signal, target_scope={"session_id": event_session}, policy_modifications=mods)
        await self.persistence.persist_signal(signal)
        async with state.lock:
            if state.finished:
                raise GovernedError("session finished while feedback was being applied")
            await self._persist_control(state, signal)
            if action == "REQUIRE_APPROVAL_FOR":
                state.approval_tools.update(tools)
            elif action == "BLOCK_TOOLS":
                state.blocked_tools.update(tools)
            elif action == "STRICT_MODE":
                state.strict = True
            elif action == "HALT_SESSION":
                state.halted = True
            # ALERT remains additive evidence; it never authorizes anything.
            # Activate the monotonic restriction before marking the signal fully
            # applied. If cancellation follows the marker commit, same-process
            # enforcement is already active; retry is still deterministic.
            await self.persistence.mark_signal_applied(signal.signal_id)
        get_logger().log("intercept", "feedback.applied", session=event_session, signal=signal.signal_id,
                         adjustment=action, plugin=signal.source_plugin, trigger=signal.trigger_event_id)

    async def _persist_control(self, state: _Run, signal: PolicyAdjustmentSignal) -> None:
        digest = hashlib.sha256(signal.signal_id.encode()).hexdigest()
        control_event_id = f"control_{digest[:32]}"
        try:
            stored = await self.persistence.wire_event(control_event_id)
            if stored.get("session_id") == state.contract.session_id:
                return
        except KeyError:
            pass
        action = ActionProposal(action_id=f"signal_{digest[:24]}", session_id=state.contract.session_id,
                               agent_id=state.contract.agent_id, tool="feedback_signal", side_effect="read",
                               transport="inproc", arguments={})
        candidate = build_action_event(
            contract=state.contract, action_type="control", action_id=action.action_id,
            name="feedback_signal", status="completed", decision="ALERT",
            wire_details={"change": "adjustment_applied", "signal_id": signal.signal_id,
                          "policy_version": state.contract.policy_version},
            reason_code="POLICY_ADJUSTMENT", intervention_id=signal.signal_id, event_id=control_event_id,
        )
        await self.persistence.append(candidate)
