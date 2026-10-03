"""Independent KYC outcome verification at the persisted session boundary.

The plugin reads the synthetic bank through a separate read-only SQLite
connection. It never trusts event arguments as the application identity and
never persists postcondition text from the underlying verifier.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import importlib.util
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from contracts import TaskContract, VerificationCheck, VerificationResult

from consume_plane.sdk import AgentAction, FindingDraft, Subscription


TERMINAL_NO_CREATE = frozenset({"edd", "rejected", "more_docs_requested"})
PASSED = "CHECK_PASSED"
NOT_APPLICABLE = "NOT_APPLICABLE"
FAILED = "POSTCONDITION_FAILED"
INCOMPLETE = "CHECK_INCOMPLETE"


def _load_postconditions():
    """Load data/postconditions.py without changing its legacy standalone imports."""
    data_dir = Path(__file__).resolve().parents[2] / "data"
    module_path = data_dir / "postconditions.py"
    module_name = "consume_plane_data_postconditions"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError("KYC verifier module unavailable")
    module = importlib.util.module_from_spec(spec)
    # postconditions.py imports `rules` for its original CLI/test use. Add only
    # the trusted repository data directory while loading it, then restore path.
    sys.path.insert(0, str(data_dir))
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if sys.path and sys.path[0] == str(data_dir):
            sys.path.pop(0)
    return module


def _contract_target(contract: TaskContract) -> str | None:
    if contract.case_id:
        return contract.case_id
    if len(contract.target_ids) == 1:
        return next(iter(contract.target_ids))
    return None


def _result(checks: list[VerificationCheck]) -> VerificationResult:
    if any(c.status == "FAIL" for c in checks):
        status = "FAILED_POSTCONDITIONS"
    elif any(c.status == "INCOMPLETE" for c in checks):
        status = "VERIFICATION_INCOMPLETE"
    else:
        status = "VERIFIED_SUCCESS"
    return VerificationResult(status, tuple(checks))


class OutcomeVerifier:
    name = "outcome-verifier"
    version = "1.0.0"
    method = "deterministic"
    subscription = Subscription(kinds=frozenset({"session"}), needs_trajectory=True)

    def __init__(self, *, bank_paths: Mapping[str, str | Path] | None = None,
                 bank_path_resolver: Callable[[str], str | Path | None] | None = None,
                 result_store: Any | None = None):
        self.bank_paths = {k: Path(v) for k, v in (bank_paths or {}).items()}
        self.bank_path_resolver = bank_path_resolver
        self.result_store = result_store
        self._postconditions = None

    async def setup(self, ctx):
        # This block is trusted deployment configuration. Constructor bindings
        # take precedence so callers can pin session-to-copy isolation.
        if not self.bank_paths:
            configured = ctx.config.get("bank_paths", {})
            if isinstance(configured, Mapping):
                self.bank_paths = {str(k): Path(v) for k, v in configured.items()}
            path = ctx.config.get("bank_path") or ctx.config.get("bank_db")
            if path:
                self._default_bank_path = Path(path)
            else:
                self._default_bank_path = None
        else:
            self._default_bank_path = None
        self._postconditions = await asyncio.to_thread(_load_postconditions)

    def _bank_path(self, session_id: str) -> Path | None:
        path = self.bank_paths.get(session_id)
        if path is None and self.bank_path_resolver is not None:
            resolved = self.bank_path_resolver(session_id)
            path = Path(resolved) if resolved is not None else None
        if path is None:
            path = getattr(self, "_default_bank_path", None)
        return path

    @staticmethod
    def _incomplete(check_id: str, code: str) -> VerificationCheck:
        return VerificationCheck(check_id, "INCOMPLETE", code)

    async def handle(self, action: AgentAction, ctx):
        if action.kind != "session" or action.payload.phase != "ended":
            return
        contract = await ctx.contract()
        if contract is None:
            result = _result([self._incomplete("KYC-OUTCOME", "TRUSTED_CONTRACT_MISSING")])
            await self._persist(action.session_id, result)
            self._emit(action, ctx, result)
            return
        target_id = _contract_target(contract)
        if (contract.session_id != action.session_id or target_id is None
                or target_id not in contract.target_ids):
            result = _result([self._incomplete("KYC-OUTCOME", "CONTRACT_TARGET_INVALID")])
            await self._persist(action.session_id, result)
            self._emit(action, ctx, result)
            return
        if action.case_id is not None and action.case_id != target_id:
            result = _result([self._incomplete("KYC-OUTCOME", "EVENT_CONTRACT_MISMATCH")])
            await self._persist(action.session_id, result)
            self._emit(action, ctx, result)
            return

        path = self._bank_path(action.session_id)
        if path is None or not path.is_file():
            result = _result([self._incomplete("KYC-OUTCOME", "BANK_STATE_UNAVAILABLE")])
            await self._persist(action.session_id, result)
            self._emit(action, ctx, result)
            return

        try:
            trajectory = await ctx.trajectory()
            expected_action_ids: list[tuple[str, str]] | None = []
            successful_screen_action_ids: list[str] = []
            recorded_actions = trajectory.actions if hasattr(trajectory, "actions") else trajectory
            for recorded in recorded_actions:
                if recorded.kind != "tool_use" or recorded.status not in {"completed", "redacted", "failed"}:
                    continue
                # Persisted result status records whether execution occurred.
                # A later output-inspection BLOCK can follow a committed effect,
                # so the gateway's final delivery verdict must not erase this ID.
                raw = recorded.raw if isinstance(recorded.raw, Mapping) else {}
                context = raw.get("context", {})
                action_id = raw.get("action_id") or (context.get("action_id") if isinstance(context, Mapping) else None)
                if not action_id:
                    expected_action_ids = None
                    break
                expected_action_ids.append((str(action_id), str(recorded.payload.tool)))
                if recorded.payload.tool == "screen_sanctions" and recorded.status in {"completed", "redacted"}:
                    successful_screen_action_ids.append(str(action_id))
            results = await asyncio.to_thread(self._verify_snapshot, path, target_id, action.session_id,
                                              contract, expected_action_ids, successful_screen_action_ids)
        except Exception:
            # Never persist SQLite exception text: it can expose values or paths.
            result = _result([self._incomplete("KYC-OUTCOME", "VERIFIER_EXECUTION_FAILED")])
        else:
            result = _result(results)
        await self._persist(action.session_id, result)
        self._emit(action, ctx, result)

    def _verify_snapshot(self, path: Path, app_id: str, session_id: str,
                         contract: TaskContract,
                         expected_action_ids: list[tuple[str, str]] | None,
                         successful_screen_action_ids: list[str] | None = None) -> list[VerificationCheck]:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA query_only = ON")
            con.execute("BEGIN")  # one consistent read snapshot for state and trace
            app_row = con.execute(
                "SELECT status, applicant_type, declared FROM onboarding_applications WHERE application_id = ?", (app_id,)
            ).fetchone()
            if app_row is None:
                return [self._incomplete("KYC-OUTCOME", "APPLICATION_STATE_MISSING")]
            trace_rows = con.execute(
                "SELECT COUNT(*) FROM audit_actions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not trace_rows or trace_rows[0] == 0:
                return [self._incomplete("KYC-OUTCOME", "PROCESS_TRACE_MISSING")]

            # The gateway pins initial task state outside the model-writable audit
            # stream. Without it, state drift and execution provenance are unknown.
            baseline_row = con.execute(
                "SELECT run_id, contract_id, case_id, baseline_json FROM governed_baselines WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if baseline_row is None:
                return [self._incomplete("KYC-BASELINE", "TRUSTED_BASELINE_MISSING")]
            if (baseline_row["run_id"] != contract.run_id or baseline_row["contract_id"] != contract.contract_id
                    or baseline_row["case_id"] != app_id):
                return [VerificationCheck("KYC-BASELINE", "FAIL", "TRUSTED_BASELINE_BINDING_MISMATCH")]
            baseline = json.loads(baseline_row["baseline_json"])
            if baseline.get("application_id") != app_id:
                return [VerificationCheck("KYC-BASELINE", "FAIL", "TRUSTED_BASELINE_BINDING_MISMATCH")]
            try:
                persisted_declared = json.loads(app_row["declared"] or "{}")
            except (TypeError, ValueError):
                return [VerificationCheck("KYC-IDENTITY", "FAIL", "PERSISTED_IDENTITY_INVALID")]
            expected_docs = sorted((str(d.get("doc_id")), str(d.get("doc_type")), d.get("expiry_date"))
                                   for d in baseline.get("documents", ()))
            persisted_docs = sorted((str(r["doc_id"]), str(r["doc_type"]), r["expiry_date"])
                                    for r in con.execute(
                                        "SELECT doc_id, doc_type, expiry_date FROM documents WHERE application_id = ?",
                                        (app_id,)).fetchall())
            registry_mismatch = False
            if app_row["applicant_type"] == "company":
                expected_registry = baseline.get("registry")
                reg_number = persisted_declared.get("reg_number")
                actual_registry = con.execute(
                    "SELECT reg_number, legal_name, status, ubos FROM company_registry WHERE reg_number = ?",
                    (reg_number,),
                ).fetchone() if reg_number else None
                if expected_registry is None or actual_registry is None:
                    registry_mismatch = expected_registry is not None or actual_registry is not None
                else:
                    try:
                        actual_ubos = json.loads(actual_registry["ubos"] or "[]")
                    except (TypeError, ValueError):
                        actual_ubos = None
                    registry_mismatch = (actual_registry["legal_name"] != expected_registry.get("legal_name")
                                         or actual_registry["status"] != expected_registry.get("status")
                                         or actual_ubos != expected_registry.get("ubos"))
            if (app_row["applicant_type"] != baseline.get("applicant_type")
                    or persisted_declared != baseline.get("declared")
                    or expected_docs != persisted_docs or registry_mismatch):
                return [VerificationCheck("KYC-IDENTITY", "FAIL", "PERSISTED_IDENTITY_MISMATCH")]
            if (not contract.feed_version or not baseline.get("screening_source_version")):
                return [self._incomplete("KYC-SCREENING-EVIDENCE", "PINNED_SCREENING_SOURCE_MISSING")]
            checks: list[VerificationCheck] = [VerificationCheck("KYC-BASELINE", "PASS", PASSED),
                                               VerificationCheck("KYC-IDENTITY", "PASS", PASSED)]
            if expected_action_ids is None:
                checks.append(self._incomplete("KYC-ACTION-PROVENANCE", "L2_ACTION_ID_MISSING"))
            else:
                try:
                    audit_ids = {str(row[0]) for row in con.execute(
                        "SELECT action_id FROM audit_actions WHERE session_id = ? AND action_id IS NOT NULL", (session_id,)
                    ).fetchall()}
                except sqlite3.OperationalError:
                    audit_ids = None
                if audit_ids is None:
                    checks.append(self._incomplete("KYC-ACTION-PROVENANCE", "BANK_ACTION_ID_UNAVAILABLE"))
                elif ({action_id for action_id, _ in expected_action_ids} == audit_ids
                      and len(expected_action_ids) == len({action_id for action_id, _ in expected_action_ids})):
                    checks.append(VerificationCheck("KYC-ACTION-PROVENANCE", "PASS", PASSED))
                else:
                    checks.append(VerificationCheck("KYC-ACTION-PROVENANCE", "FAIL", "ACTION_ID_SET_MISMATCH"))

            calls = self._postconditions.calls_from_audit(con, session_id)
            # Existing verifier returns raw explanatory text. Keep only trusted
            # check IDs and outcomes; details are replaced with fixed codes.
            raw_checks = self._postconditions.verify_onboarding(con, app_id, calls)
            checks.extend(VerificationCheck(
                item["id"],
                "PASS" if item["ok"] is True or item["ok"] is None else "FAIL",
                PASSED if item["ok"] is True else (NOT_APPLICABLE if item["ok"] is None else FAILED),
            ) for item in raw_checks if item["id"] not in {"ONB-P1", "ONB-P6"})

            status = app_row["status"]
            create_calls = sum(call.get("tool") == "create_client" for call in calls)
            count = con.execute(
                "SELECT COUNT(*) FROM clients WHERE application_id = ?", (app_id,)
            ).fetchone()[0]
            clients = con.execute(
                "SELECT client_id, full_name, date_of_birth, client_type FROM clients WHERE application_id = ?", (app_id,)
            ).fetchall()
            accounts = con.execute(
                "SELECT a.account_id, a.client_id FROM accounts a JOIN clients c ON c.client_id = a.client_id WHERE c.application_id = ?",
                (app_id,),
            ).fetchall()
            if status == "approved":
                if count == 1:
                    checks.append(VerificationCheck("KYC-CLIENT-COUNT", "PASS", PASSED))
                else:
                    checks.append(VerificationCheck("KYC-CLIENT-COUNT", "FAIL", "APPROVED_CLIENT_COUNT_INVALID"))
                if create_calls == 1:
                    checks.append(VerificationCheck("KYC-CREATE-TRACE", "PASS", PASSED))
                else:
                    checks.append(VerificationCheck("KYC-CREATE-TRACE", "FAIL", "APPROVED_CREATE_TRACE_INVALID"))
                if len(accounts) == 1 and len(clients) == 1 and accounts[0]["client_id"] == clients[0]["client_id"]:
                    checks.append(VerificationCheck("KYC-ACCOUNT-LINK", "PASS", PASSED))
                else:
                    checks.append(VerificationCheck("KYC-ACCOUNT-LINK", "FAIL", "APPROVED_ACCOUNT_LINK_INVALID"))
                if len(clients) == 1:
                    declared = baseline.get("declared") or {}
                    applicant_name = (baseline.get("registry") or {}).get("legal_name") if baseline.get("applicant_type") == "company" else declared.get("name")
                    applicant_dob = None if baseline.get("applicant_type") == "company" else declared.get("date_of_birth")
                    identity_matches = (clients[0]["full_name"] == applicant_name
                                        and clients[0]["date_of_birth"] == applicant_dob
                                        and clients[0]["client_type"] == baseline.get("applicant_type"))
                    checks.append(VerificationCheck("KYC-CLIENT-IDENTITY", "PASS" if identity_matches else "FAIL",
                                                    PASSED if identity_matches else "CREATED_CLIENT_IDENTITY_MISMATCH"))
                else:
                    checks.append(VerificationCheck("KYC-CLIENT-IDENTITY", "FAIL", "APPROVED_CLIENT_COUNT_INVALID"))
                # A durable effect receipt must bind all persisted outputs to this
                # run and exact contract policy. Missing receipt is incomplete;
                # a receipt that contradicts observed state is a failed outcome.
                try:
                    receipt_rows = con.execute(
                        "SELECT receipt_id, source_event_id, application_id, run_id, action_id, command_digest, client_id, account_id, policy_version, policy_hash FROM effect_receipts WHERE application_id = ?",
                        (app_id,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    receipt_rows = None
                if receipt_rows is None or not receipt_rows:
                    checks.append(VerificationCheck("KYC-EFFECT-RECEIPT", "INCOMPLETE", "EFFECT_RECEIPT_MISSING"))
                elif len(receipt_rows) != 1:
                    checks.append(VerificationCheck("KYC-EFFECT-RECEIPT", "FAIL", "EFFECT_RECEIPT_NOT_UNIQUE"))
                else:
                    receipt = receipt_rows[0]
                    create_action_ids = {action_id for action_id, tool in (expected_action_ids or ())
                                         if tool == "create_client"}
                    receipt_ok = (len(clients) == 1 and len(accounts) == 1
                                  and receipt["client_id"] == clients[0]["client_id"]
                                  and receipt["account_id"] == accounts[0]["account_id"]
                                  and receipt["run_id"] == contract.run_id
                                  and receipt["policy_version"] == contract.policy_version
                                  and receipt["policy_hash"] == contract.policy_hash
                                  and len(create_action_ids) == 1 and receipt["action_id"] in create_action_ids
                                  and bool(receipt["source_event_id"])
                                  and bool(receipt["command_digest"]))
                    checks.append(VerificationCheck("KYC-EFFECT-RECEIPT", "PASS" if receipt_ok else "FAIL",
                                                    PASSED if receipt_ok else "EFFECT_RECEIPT_BINDING_MISMATCH"))
                checks.extend(self._verify_screening_evidence(
                    con, session_id, contract, baseline, expected_action_ids,
                    successful_screen_action_ids,
                ))
            elif status in TERMINAL_NO_CREATE:
                if count == 0:
                    checks.append(VerificationCheck("KYC-CLIENT-COUNT", "PASS", PASSED))
                else:
                    checks.append(VerificationCheck("KYC-CLIENT-COUNT", "FAIL", "TERMINAL_STATE_HAS_CLIENT"))
                if create_calls == 0:
                    checks.append(VerificationCheck("KYC-CREATE-TRACE", "PASS", PASSED))
                else:
                    checks.append(VerificationCheck("KYC-CREATE-TRACE", "FAIL", "TERMINAL_STATE_HAS_CREATE_TRACE"))
                if len(accounts) == 0:
                    checks.append(VerificationCheck("KYC-ACCOUNT-LINK", "PASS", PASSED))
                else:
                    checks.append(VerificationCheck("KYC-ACCOUNT-LINK", "FAIL", "TERMINAL_STATE_HAS_ACCOUNT"))
                checks.append(VerificationCheck("KYC-EFFECT-RECEIPT", "PASS", NOT_APPLICABLE))
            else:
                checks.append(VerificationCheck("KYC-CLIENT-COUNT", "INCOMPLETE", "APPLICATION_NOT_TERMINAL"))
                checks.append(VerificationCheck("KYC-CREATE-TRACE", "INCOMPLETE", "APPLICATION_NOT_TERMINAL"))
            return checks
        finally:
            try:
                con.rollback()
            finally:
                con.close()

    def _verify_screening_evidence(self, con: sqlite3.Connection, session_id: str,
                                   contract: TaskContract, baseline: Mapping[str, Any],
                                   expected_action_ids: list[tuple[str, str]] | None,
                                   successful_screen_action_ids: list[str] | None) -> list[VerificationCheck]:
        def incomplete(code: str) -> list[VerificationCheck]:
            return [self._incomplete("KYC-SCREENING-EVIDENCE", code),
                    self._incomplete("ONB-P1", code), self._incomplete("ONB-P6", code)]

        if expected_action_ids is None or successful_screen_action_ids is None:
            return incomplete("L2_SCREENING_ACTION_ID_MISSING")
        try:
            evidence_rows = con.execute(
                "SELECT session_id,run_id,action_id,subject_hash,source_version,occurred_at,hit_count,max_score,decision "
                "FROM governed_screening_evidence WHERE session_id=? AND run_id=?",
                (session_id, contract.run_id),
            ).fetchall()
            audit_rows = con.execute(
                "SELECT action_id,tool,ts,args_json,result_json FROM audit_actions WHERE session_id=?",
                (session_id,),
            ).fetchall()
            source_rows = []
            for table in ("sanctions_list", "pep_list"):
                source_rows.append((table, [tuple(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY 1")]))
        except sqlite3.OperationalError:
            return incomplete("SCREENING_PROVENANCE_UNAVAILABLE")

        current_source_version = hashlib.sha256(
            json.dumps(source_rows, sort_keys=True, default=str).encode()
        ).hexdigest()
        pinned_source_version = baseline.get("screening_source_version")
        if current_source_version != pinned_source_version:
            return [VerificationCheck("KYC-SCREENING-EVIDENCE", "FAIL", "SCREENING_SOURCE_VERSION_MISMATCH"),
                    VerificationCheck("ONB-P1", "INCOMPLETE", "SCREENING_SOURCE_VERSION_MISMATCH"),
                    VerificationCheck("ONB-P6", "INCOMPLETE", "SCREENING_SOURCE_VERSION_MISMATCH")]

        audit_by_id = {str(row["action_id"]): row for row in audit_rows if row["action_id"] is not None}
        l2_tools = {action_id: tool for action_id, tool in expected_action_ids}
        ordered_ids = [action_id for action_id, _ in expected_action_ids]
        create_ids = {aid for aid, tool in expected_action_ids if tool == "create_client"}
        if len(create_ids) != 1:
            return incomplete("CREATE_ACTION_PROVENANCE_MISSING")
        create_id = next(iter(create_ids))
        create_audit = audit_by_id.get(create_id)
        if create_audit is None:
            return incomplete("CREATE_AUDIT_PROVENANCE_MISSING")
        try:
            create_result = json.loads(create_audit["result_json"] or "{}")
        except (TypeError, ValueError):
            return incomplete("CREATE_AUDIT_RESULT_INVALID")
        if create_audit["tool"] != "create_client" or "error" in create_result:
            return incomplete("CREATE_AUDIT_NOT_SUCCESSFUL")

        registry = baseline.get("registry") or {}
        declared = baseline.get("declared") or {}
        applicant_name = registry.get("legal_name") if baseline.get("applicant_type") == "company" else declared.get("name")
        applicant_dob = None if baseline.get("applicant_type") == "company" else declared.get("date_of_birth")
        subjects: list[tuple[str, str | None, str]] = [(str(applicant_name or ""), applicant_dob, "applicant")]
        if baseline.get("applicant_type") == "company":
            subjects.extend((str(ubo.get("name") or ""), ubo.get("dob"), "ubo")
                            for ubo in registry.get("ubos", ()) if ubo.get("ownership_pct", 0) >= 25)
        if any(not name for name, _, _ in subjects):
            return incomplete("SCREENING_SUBJECT_MISSING_FROM_BASELINE")
        expected_subject_hashes = {hashlib.sha256(f"{name.strip().casefold()}|{dob}".encode()).hexdigest()
                                   for name, dob, _ in subjects}

        try:
            rules = self._postconditions
            sanctions = rules._entries(con, "sanctions_list")
            pep = rules._entries(con, "pep_list")
        except Exception:
            return incomplete("SCREENING_SOURCE_UNAVAILABLE")

        evidence_by_subject: dict[str, list[sqlite3.Row]] = {}
        for row in evidence_rows:
            evidence_by_subject.setdefault(str(row["subject_hash"]), []).append(row)
        failures: list[str] = (["SCREENING_SUBJECT_BINDING_MISMATCH"]
                               if any(subject_hash not in expected_subject_hashes
                                      for subject_hash in evidence_by_subject) else [])
        missing = False
        sanctions_hits = {"applicant": False, "ubo": False}
        for name, dob, kind in subjects:
            subject_hash = hashlib.sha256(f"{name.strip().casefold()}|{dob}".encode()).hexdigest()
            rows = evidence_by_subject.get(subject_hash, [])
            if not rows:
                missing = True
                continue
            expected_hits = []
            for tag, entries in (("sanctions", sanctions), ("pep", pep)):
                expected_hits.extend({"list": tag, **hit} for hit in rules.screen(name, dob, entries))
            expected_hits.sort(key=lambda hit: -hit["score"])
            expected_count = len(expected_hits)
            expected_max = max((float(hit["score"]) for hit in expected_hits), default=0.0)
            expected_decision = ("HIT" if expected_max >= rules.MATCH_THRESHOLD else
                                 "POSSIBLE" if expected_hits else "CLEAR")
            if any(hit["list"] == "sanctions" and hit["score"] >= rules.MATCH_THRESHOLD for hit in expected_hits):
                sanctions_hits[kind] = True
            valid_count = 0
            for evidence in rows:
                action_id = str(evidence["action_id"])
                if evidence["source_version"] != pinned_source_version:
                    failures.append("SCREENING_SOURCE_VERSION_MISMATCH")
                    continue
                if (action_id not in successful_screen_action_ids
                        or l2_tools.get(action_id) != "screen_sanctions"
                        or action_id not in audit_by_id):
                    failures.append("SCREENING_ACTION_BINDING_MISMATCH")
                    continue
                audit = audit_by_id[action_id]
                if audit["tool"] != "screen_sanctions":
                    failures.append("SCREENING_ACTION_BINDING_MISMATCH")
                    continue
                try:
                    audit_args = json.loads(audit["args_json"] or "{}")
                    audit_result = json.loads(audit["result_json"] or "{}")
                    audit_max = float(audit_result.get("max_score", -1))
                except (TypeError, ValueError):
                    failures.append("SCREENING_AUDIT_RESULT_INVALID")
                    continue
                if (audit_args.get("subject_hash") != subject_hash or "error" in audit_result
                        or audit_result.get("hit_count") != expected_count
                        or audit_max != expected_max
                        or sorted(audit_result.get("lists", [])) != sorted({h["list"] for h in expected_hits})
                        or int(evidence["hit_count"]) != expected_count
                        or float(evidence["max_score"]) != expected_max
                        or evidence["decision"] != expected_decision):
                    failures.append("SCREENING_RESULT_MISMATCH")
                    continue
                if (action_id not in ordered_ids or ordered_ids.index(action_id) >= ordered_ids.index(create_id)):
                    failures.append("SCREENING_AFTER_CREATE")
                    continue
                try:
                    evidence_at = datetime.fromisoformat(str(evidence["occurred_at"]).replace("Z", "+00:00"))
                    audit_at = datetime.fromisoformat(str(audit["ts"]).replace("Z", "+00:00"))
                    create_at = datetime.fromisoformat(str(create_audit["ts"]).replace("Z", "+00:00"))
                    baseline_at = datetime.fromisoformat(str(
                        con.execute("SELECT captured_at FROM governed_baselines WHERE session_id=?", (session_id,)).fetchone()[0]
                    ).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    failures.append("SCREENING_CHRONOLOGY_INVALID")
                    continue
                if evidence_at < baseline_at or evidence_at > audit_at or evidence_at >= create_at or audit_at >= create_at:
                    failures.append("SCREENING_CHRONOLOGY_INVALID")
                    continue
                valid_count += 1
            if valid_count == 0:
                missing = True

        if failures:
            code = failures[0]
            return [VerificationCheck("KYC-SCREENING-EVIDENCE", "FAIL", code),
                    VerificationCheck("ONB-P1", "FAIL" if sanctions_hits["applicant"] else "INCOMPLETE",
                                      "SANCTIONS_HIT_NOT_MITIGATED" if sanctions_hits["applicant"] else "SCREENING_PROVENANCE_INVALID"),
                    VerificationCheck("ONB-P6", "FAIL" if sanctions_hits["ubo"] else "INCOMPLETE",
                                      "UBO_SANCTIONS_HIT_NOT_MITIGATED" if sanctions_hits["ubo"] else "SCREENING_PROVENANCE_INVALID")]
        if missing:
            return incomplete("SCREENING_EVIDENCE_MISSING")
        return [VerificationCheck("KYC-SCREENING-EVIDENCE", "PASS", PASSED),
                VerificationCheck("ONB-P1", "FAIL" if sanctions_hits["applicant"] else "PASS",
                                  "SANCTIONS_HIT_NOT_MITIGATED" if sanctions_hits["applicant"] else PASSED),
                VerificationCheck("ONB-P6", "FAIL" if sanctions_hits["ubo"] else "PASS",
                                  "UBO_SANCTIONS_HIT_NOT_MITIGATED" if sanctions_hits["ubo"] else PASSED)]

    async def _persist(self, session_id: str, result: VerificationResult) -> None:
        if self.result_store is not None:
            await self.result_store.write_verification(session_id, result.to_dict())

    @staticmethod
    def _emit(action: AgentAction, ctx, result: VerificationResult) -> None:
        severity = "high" if result.verification_status == "FAILED_POSTCONDITIONS" else "medium"
        if result.verification_status == "VERIFIED_SUCCESS":
            severity = "low"
        ctx.emit_finding(FindingDraft(
            rule_id="outcome.kyc_verification",
            severity=severity,
            summary=f"KYC outcome verification: {result.verification_status}",
            evidence_event_ids=(action.event_id,),
            details={"verification_status": result.verification_status,
                     "checks": [{"id": c.id, "status": c.status, "detail": c.detail} for c in result.checks]},
        ))

    async def result(self, session_id: str) -> VerificationResult | None:
        """Fetch the durable, sanitized verification result for a session."""
        if self.result_store is None:
            return None
        raw = await self.result_store.get_verification(session_id)
        if raw is None:
            return None
        if isinstance(raw, VerificationResult):
            return raw
        checks = tuple(VerificationCheck(
            id=item["id"], status=item["status"], detail=item.get("detail", "STORED_RESULT"))
            for item in raw.get("checks", ()))
        return VerificationResult(raw["verification_status"], checks)
