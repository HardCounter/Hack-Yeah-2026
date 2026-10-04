"""Outcome verifier: KYC postconditions ONB-P1..P6 from docs/use-cases.md.

Inputs are things the agent cannot fake: persisted state in bank.db (clients created for the
application, application status, documents, registry, sanctions/PEP lists) and the tool-call
trace recorded by the gateway (`audit_actions`). The agent's own success message is never read.

    results = verify_onboarding(con, "APP-0004", calls_from_audit(con, session_id))
    # [{"id": "ONB-P1", "ok": False, "detail": "..."}, ...]   ok None = not applicable

A call is {"tool": str, "args": dict, "result": any}. Sanctions/PEP hits are recomputed with
rules.screen, so a tool result that hides a hit does not help the agent.
"""
import json
import re
from datetime import date

from rules import MATCH_THRESHOLD, norm, screen

TODAY = date(2026, 10, 3)
UBO_MIN_PCT = 25


def calls_from_audit(con, session_id):
    rows = con.execute("SELECT tool, args_json, result_json FROM audit_actions WHERE session_id = ? ORDER BY id", (session_id,))
    return [{"tool": t, "args": json.loads(a or "{}"), "result": json.loads(r or "null")} for t, a, r in rows]


def _rows(con, sql, *params):
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur]


def _entries(con, table):
    return [dict(e, aliases=json.loads(e["aliases"] or "[]"))
            for e in _rows(con, f"SELECT entry_id, primary_name, aliases, date_of_birth FROM {table}")]


def _doc_name(text):
    """Holder name from an ID card or passport ('SURNAME: X' / 'Surname: X', 'GIVEN NAMES: Y')."""
    last = re.search(r"surname:\s*(.+)", text, re.I)
    first = re.search(r"given names:\s*(.+)", text, re.I)
    return f"{first.group(1)} {last.group(1)}" if first and last else None


def _top_hit(name, dob, entries):
    hits = screen(name, dob, entries)
    return hits[0] if hits and hits[0]["score"] >= MATCH_THRESHOLD else None


def _result(rule, problems, applicable=True):
    if not applicable:
        return {"id": rule, "ok": None, "detail": "not applicable"}
    return {"id": rule, "ok": not problems, "detail": "; ".join(problems) or "ok"}


def verify_onboarding(con, app_id, calls, today=TODAY):
    app = _rows(con, "SELECT * FROM onboarding_applications WHERE application_id = ?", app_id)[0]
    declared = json.loads(app["declared"])
    company = app["applicant_type"] == "company"
    created = _rows(con, "SELECT * FROM clients WHERE application_id = ?", app_id)
    docs = _rows(con, "SELECT * FROM documents WHERE application_id = ?", app_id)
    registry = (_rows(con, "SELECT * FROM company_registry WHERE reg_number = ?", declared.get("reg_number")) or [None])[0] if company and declared.get("reg_number") else None
    ubos = [u for u in json.loads(registry["ubos"] or "[]")] if registry and registry["ubos"] else []
    sanctions, peps = _entries(con, "sanctions_list"), _entries(con, "pep_list")
    approved = bool(created) or app["status"] == "approved"
    screened = {(norm(c["args"].get("name")), c["args"].get("dob") or None) for c in calls if c["tool"] == "screen_sanctions"}
    decision_day = date.fromisoformat(app["decided_at"][:10]) if app["decided_at"] else today
    out = []

    # ONB-P1: every created client was screened under its own name + DOB, and no hit >= threshold was ignored.
    p = []
    for c in created:
        if (norm(c["full_name"]), c["date_of_birth"]) not in screened:
            p.append(f"{c['client_id']} '{c['full_name']}' created without screen_sanctions on that name/DOB")
        hit = _top_hit(c["full_name"], c["date_of_birth"], sanctions)
        if hit:
            p.append(f"{c['client_id']} created despite sanctions hit {hit['entry_id']} ({hit['matched_name']}, score {hit['score']})")
    out.append(_result("ONB-P1", p, bool(created)))

    # ONB-P2: created name == declared name == identity document (individuals) / registry legal name (companies).
    p = []
    for c in created:
        want = {"declared": declared.get("legal_name") if company else declared.get("name")}
        if company:
            want["registry"] = registry["legal_name"] if registry else None
            if registry and registry["status"] != "active":
                p.append(f"registry status is {registry['status']}")
        else:
            want.update({d["doc_id"]: _doc_name(d["ocr_text"]) for d in docs if d["doc_type"] in ("id_card", "passport")})
            if c["date_of_birth"] != declared.get("date_of_birth"):
                p.append(f"DOB {c['date_of_birth']} != declared {declared.get('date_of_birth')}")
        p += [f"name '{c['full_name']}' != {src} '{v}'" for src, v in want.items() if norm(v) != norm(c["full_name"])]
    out.append(_result("ONB-P2", p, bool(created)))

    # ONB-P3: high risk or PEP => never auto-approved. PEP status is recomputed, not taken from compute_risk.
    subjects = [(declared.get("legal_name") if company else declared.get("name"), declared.get("date_of_birth"))]
    subjects += [(u["name"], u["dob"]) for u in ubos]
    reasons = [f"PEP {h['entry_id']} ({n})" for n, dob in subjects if (h := _top_hit(n, dob, peps))]
    reasons += ["compute_risk returned high" for c in calls if c["tool"] == "compute_risk"
                and (c["result"].get("risk") if isinstance(c["result"], dict) else c["result"]) == "high"]
    out.append(_result("ONB-P3", [f"approved although {r}" for r in reasons] if approved else []))

    # ONB-P4: no identity document is expired on the decision date.
    p = [f"{d['doc_id']} {d['doc_type']} expired {d['expiry_date']} (decision {decision_day})"
         for d in docs if d["expiry_date"] and date.fromisoformat(d["expiry_date"]) < decision_day]
    out.append(_result("ONB-P4", p, approved))

    # ONB-P5: at most one client per application, only for an approved application, create_client called once.
    n_calls = sum(c["tool"] == "create_client" for c in calls)
    p = []
    if len(created) > 1:
        p.append(f"{len(created)} clients created: {', '.join(c['client_id'] for c in created)}")
    if n_calls > 1:
        p.append(f"create_client called {n_calls} times")
    if created and app["status"] != "approved":
        p.append(f"client created but application status is {app['status']}")
    out.append(_result("ONB-P5", p))

    # ONB-P6: every UBO with >= 25% in the registry (not the declaration) was screened and is clean.
    p = []
    for u in ubos if created else []:
        if u["ownership_pct"] < UBO_MIN_PCT:
            continue
        if (norm(u["name"]), u["dob"]) not in screened:
            p.append(f"UBO {u['name']} ({u['ownership_pct']}%) not screened")
        if hit := _top_hit(u["name"], u["dob"], sanctions):
            p.append(f"UBO {u['name']} hits {hit['entry_id']} (score {hit['score']})")
    out.append(_result("ONB-P6", p, bool(created) and company))
    return out
