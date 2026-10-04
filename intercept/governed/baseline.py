"""Protected baseline of the target application, captured from the pinned bank copy at session start.

The gateway checks proposals against it (identity, scope, screening subjects) and the outcome verifier
reads the persisted copy; the agent never supplies these facts.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from contracts import TaskContract
from intercept.governed import GovernedError
from persistence import generate_utc_iso_timestamp
from persistence.business import ensure_business_schema


@dataclass
class Baseline:
    """Protected facts about the target application, captured once at session start."""
    app_id: str
    applicant_type: str
    declared: dict[str, Any]
    status: str
    documents: dict[str, dict[str, Any]]
    registry: dict[str, Any] | None
    subjects: dict[tuple[str, str | None], dict[str, Any]]
    screening_source_version: str


def norm_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    from rules import norm
    return norm(name)


def screening_source_version(con: sqlite3.Connection) -> str:
    rows = []
    for table in ("sanctions_list", "pep_list"):
        rows.append((table, [tuple(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY 1")]))
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def ensure_governed_schema(database: Path) -> None:
    with sqlite3.connect(database, timeout=5) as con:
        con.execute("BEGIN IMMEDIATE")
        ensure_business_schema(con)
        columns = {row[1] for row in con.execute("PRAGMA table_info(audit_actions)")}
        if "action_id" not in columns:
            con.execute("ALTER TABLE audit_actions ADD COLUMN action_id TEXT")
        con.execute("""CREATE TABLE IF NOT EXISTS governed_baselines (
            session_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, contract_id TEXT NOT NULL,
            case_id TEXT NOT NULL, baseline_json TEXT NOT NULL, captured_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS governed_screening_evidence (
            session_id TEXT NOT NULL, run_id TEXT NOT NULL, action_id TEXT NOT NULL,
            subject_hash TEXT NOT NULL, source_version TEXT NOT NULL, occurred_at TEXT NOT NULL,
            hit_count INTEGER NOT NULL, max_score REAL NOT NULL, decision TEXT NOT NULL,
            PRIMARY KEY(session_id, action_id))""")


def persist_baseline(database: Path, contract: TaskContract, baseline: Baseline) -> None:
    value = {
        "application_id": baseline.app_id, "applicant_type": baseline.applicant_type,
        "status": baseline.status, "declared": baseline.declared,
        "documents": sorted(baseline.documents.values(), key=lambda item: item["doc_id"]),
        "registry": baseline.registry,
        "screening_source_version": baseline.screening_source_version,
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(database, timeout=5) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT run_id,contract_id,case_id,baseline_json FROM governed_baselines WHERE session_id=?",
                          (contract.session_id,)).fetchone()
        expected = (contract.run_id, contract.contract_id, contract.case_id, raw)
        if row and tuple(row) != expected:
            raise GovernedError("immutable governed baseline conflicts with prior capture")
        if not row:
            con.execute("INSERT INTO governed_baselines VALUES(?,?,?,?,?,?)",
                        (contract.session_id, contract.run_id, contract.contract_id, contract.case_id,
                         raw, generate_utc_iso_timestamp()))


def load_baseline(database: Path, contract: TaskContract) -> Baseline:
    app_id = contract.case_id
    if app_id not in contract.target_ids:
        raise GovernedError("case is outside the TaskContract target scope")
    with sqlite3.connect(database, timeout=5) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT applicant_type, status, declared FROM onboarding_applications WHERE application_id=?",
            (app_id,),
        ).fetchone()
        if row is None:
            raise GovernedError("TaskContract case does not exist in the pinned bank state")
        declared = json.loads(row["declared"])
        documents = {
            d["doc_id"]: dict(d) for d in con.execute(
                "SELECT doc_id, doc_type, expiry_date FROM documents WHERE application_id=?", (app_id,)
            )
        }
        registry = None
        if row["applicant_type"] == "company":
            reg_number = declared.get("reg_number")
            reg = con.execute(
                "SELECT legal_name,status,ubos FROM company_registry WHERE reg_number=?", (reg_number,)
            ).fetchone()
            if reg:
                registry = {"legal_name": reg["legal_name"], "status": reg["status"],
                            "ubos": json.loads(reg["ubos"] or "[]")}
        expected_name = declared.get("legal_name") if row["applicant_type"] == "company" else declared.get("name")
        expected_dob = None if row["applicant_type"] == "company" else declared.get("date_of_birth")
        subjects: dict[tuple[str, str | None], dict[str, Any]] = {}
        if expected_name:
            subjects[(norm_name(expected_name), expected_dob)] = {"kind": "applicant", "name": expected_name, "dob": expected_dob}
        if registry and registry.get("legal_name"):
            subjects[(norm_name(registry["legal_name"]), None)] = {
                "kind": "applicant", "name": registry["legal_name"], "dob": None,
            }
        if registry:
            for ubo in registry["ubos"]:
                if ubo.get("ownership_pct", 0) >= 25 and ubo.get("name"):
                    subjects[(norm_name(ubo["name"]), ubo.get("dob"))] = {"kind": "ubo", "name": ubo["name"], "dob": ubo.get("dob")}
        return Baseline(app_id, row["applicant_type"], declared, row["status"], documents,
                         registry, subjects, screening_source_version(con))
