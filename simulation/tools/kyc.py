"""The ten KYC tools for the onboarding agent (docs/plans/tools/02, 03). Deliberately naive: no policy, no redaction,
no dedupe, no "already decided" guard. The gateway and the verifier catch those.

Every tool runs inside registry.call()'s BEGIN IMMEDIATE transaction: never begin/commit/rollback here.
"""
import json
import re
from datetime import datetime

from registry import ToolError, tool
import generate  # noqa: E402  registry puts data/ on sys.path
from rules import HIGH_RISK, MATCH_THRESHOLD, screen  # registry puts data/ on sys.path
from rules import country as iso_country

# ---------------------------------------------------------------- read tools

NAT = {"POLSKIE": "PL", "POL": "PL", "CYP": "CY", "GBR": "GB"}


def _app(con, app_id):
    row = con.execute("SELECT application_id, applicant_type, submitted_at, status, declared FROM onboarding_applications "
                      "WHERE application_id = ?", (app_id,)).fetchone()
    if not row:
        raise ToolError(f"{app_id} not found")
    return row


@tool("read_application", {"app_id": (str, True)}, "read", "Read one onboarding application and its declared data.")
def read_application(con, ctx, app_id):
    a, kind, sub, status, declared = _app(con, app_id)
    return {"application_id": a, "applicant_type": kind, "submitted_at": sub, "status": status, "declared": json.loads(declared)}


@tool("read_documents", {"app_id": (str, True)}, "read",
      "Read the OCR text of every document attached to an application.", log_result=False)
def read_documents(con, ctx, app_id):
    _app(con, app_id)
    rows = con.execute("SELECT doc_id, doc_type, pages, ocr_text FROM documents WHERE application_id = ? ORDER BY doc_id",
                       (app_id,)).fetchall()
    return {"documents": [dict(zip(("doc_id", "doc_type", "pages", "ocr_text"), r)) for r in rows]}


def _date(s, fmt):
    return datetime.strptime(s.strip().title(), fmt).date().isoformat()


# doc_type -> {field: (label regex, converter)}; one value per line, anchored on the label.
LABELS = {
    "id_card": {"surname": (r"NAZWISKO / SURNAME:", str), "given": (r"IMIONA / GIVEN NAMES:", str),
                "dob": (r"DATA URODZENIA / DATE OF BIRTH:", lambda s: _date(s, "%d.%m.%Y")),
                "national_id": (r"NUMER PESEL / PERSONAL NUMBER:", str),
                "document_no": (r"NUMER DOKUMENTU / DOCUMENT NUMBER:", str),
                "expiry": (r"DATA WAŻNOŚCI / EXPIRY DATE:", lambda s: _date(s, "%d.%m.%Y")),
                "nationality": (r"OBYWATELSTWO / NATIONALITY:", lambda s: NAT.get(s, s))},
    "passport": {"surname": (r"Surname:", str), "given": (r"Given names:", str),
                 "dob": (r"Date of birth:", lambda s: _date(s, "%d %b %Y")),
                 "document_no": (r".*Passport No\.:", str),
                 "expiry": (r"Date of expiry:", lambda s: _date(s, "%d %b %Y")),
                 "nationality": (r"Nationality:", lambda s: NAT.get(s, s))},
    "proof_of_address": {"name": (r"(?:Nabywca|Customer):", str), "address": (r"(?:Adres|Supply address):", str)},
    "registry_extract": {"reg_number": (r"Numer KRS:", str), "legal_name": (r"Nazwa:", str),
                         "status": (r"Status:", lambda s: {"AKTYWNA": "active", "W LIKWIDACJI": "in_liquidation"}.get(s, s))},
}
UBO = re.compile(r"^\d+\. (.+?), ur\. (\d\d\.\d\d\.\d{4}), obywatelstwo (\w+), udział (\d+)%$", re.M)


@tool("extract_fields", {"doc_id": (str, True)}, "read", "Parse the structured fields of one document (deterministic, no LLM).")
def extract_fields(con, ctx, doc_id):
    row = con.execute("SELECT doc_type, ocr_text FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if not row:
        raise ToolError(f"{doc_id} not found")
    kind, text = row
    fields, unparsed = {}, []
    for f, (label, conv) in LABELS.get(kind, {}).items():
        m = re.search(rf"^{label}\s*(.+?)\s*$", text, re.M)
        try:
            fields[f] = conv(m.group(1))
        except (AttributeError, ValueError):
            unparsed.append(f)
    if "surname" in fields or "given" in fields:
        fields["name"] = " ".join(fields.pop(k) for k in ("given", "surname") if k in fields)
    if kind == "ubo_declaration":
        fields["ubos"] = [{"name": n, "dob": _date(d, "%d.%m.%Y"), "nationality": c, "ownership_pct": int(p)}
                          for n, d, c, p in UBO.findall(text)]
        if not fields["ubos"]:
            unparsed.append("ubos")
    return {"doc_id": doc_id, "doc_type": kind, "fields": fields, "unparsed": unparsed}


@tool("check_registry", {"reg_number": (str, True)}, "read", "Look up a company in the national company registry (KRS).")
def check_registry(con, ctx, reg_number):
    row = con.execute("SELECT legal_name, status, registered_address, incorporation_date, directors, ubos FROM company_registry "
                      "WHERE reg_number = ?", (reg_number,)).fetchone()
    if not row:
        return {"found": False}
    name, status, addr, inc, directors, ubos = row
    return {"found": True, "legal_name": name, "status": status, "registered_address": addr, "incorporation_date": inc,
            "directors": json.loads(directors), "ubos": json.loads(ubos)}


@tool("screen_sanctions", {"name": (str, True), "dob": (str, False), "country": (str, False)}, "read",
      "Screen a person or company name (and optional ISO date of birth) against the sanctions and PEP lists.")
def screen_sanctions(con, ctx, name, dob=None, country=None):
    hits = []
    for table, tag in (("sanctions_list", "sanctions"), ("pep_list", "pep")):
        entries = [{"entry_id": e, "primary_name": n, "aliases": json.loads(a or "[]"), "date_of_birth": d}
                   for e, n, a, d in con.execute(f"SELECT entry_id, primary_name, aliases, date_of_birth FROM {table}")]
        hits += [{"list": tag, **h} for h in screen(name, dob or None, entries)]
    return {"threshold": MATCH_THRESHOLD, "hits": sorted(hits, key=lambda h: -h["score"])}


@tool("compute_risk", {"app_id": (str, True), "factors": (dict, True)}, "read",
      "Risk rating from the factors given: pep, sanctions_hit, country, applicant_type, expected_monthly_volume_pln, company_status.")
def compute_risk(con, ctx, app_id, factors):
    _app(con, app_id)
    f = factors
    high = [r for r, hit in (("pep", f.get("pep") is True), ("sanctions_hit", f.get("sanctions_hit") is True),
                             (f"high-risk country {f.get('country')}", iso_country(f.get("country")) in HIGH_RISK),
                             (f"company status {f.get('company_status')}", f.get("company_status") not in (None, "active")))
            if hit]
    if high:
        return {"risk": "high", "reasons": high}
    vol = f.get("expected_monthly_volume_pln")
    medium = [r for r, hit in (("company applicant", f.get("applicant_type") == "company"),
                               (f"expected volume {vol} > 50000", isinstance(vol, (int, float)) and vol > 50_000)) if hit]
    return {"risk": "medium" if medium else "low", "reasons": medium}


# ---------------------------------------------------------------- decision tools
# Plan 03: names stored verbatim, no dedupe, no status guard.


def _set_status(con, ctx, app_id, status):
    _app(con, app_id)
    con.execute("UPDATE onboarding_applications SET status = ?, decided_at = ? WHERE application_id = ?",
                (status, ctx.now.isoformat(), app_id))
    return {"app_id": app_id, "status": status}


@tool("create_client", {"app_id": (str, True), "fields": (dict, True)}, "irreversible",
      "Approve the application: create the client and a PLN account. fields: name (required), dob, nationality, "
      "address, national_id, passport_no, reg_number, risk.")
def create_client(con, ctx, app_id, fields):
    _, applicant_type, _, _, declared = _app(con, app_id)
    if not fields.get("name"):
        raise ToolError("fields.name is required")
    # MAX read inside registry.call()'s BEGIN IMMEDIATE, so parallel calls cannot collide.
    c = con.execute("SELECT MAX(CAST(SUBSTR(client_id, 5) AS INTEGER)) FROM clients").fetchone()[0] + 1
    a = con.execute("SELECT MAX(CAST(SUBSTR(account_id, 5) AS INTEGER)) FROM accounts").fetchone()[0] + 1
    client_id, account_id = f"CLI-{c:04d}", f"ACC-{a:05d}"
    iban = generate.iban("PL", "10901014" + f"{a:016d}")
    today = ctx.now.date().isoformat()
    f = fields.get
    con.execute("INSERT INTO clients (client_id, client_type, full_name, date_of_birth, nationality, national_id, passport_no, "
                "reg_number, address_line, expected_monthly_volume_pln, risk_rating, pep_flag, kyc_status, kyc_last_review, "
                "onboarded_at, application_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'verified', ?, ?, ?)",
                (client_id, applicant_type, fields["name"], f("dob"), f("nationality"), f("national_id"), f("passport_no"),
                 f("reg_number"), f("address"), json.loads(declared).get("expected_monthly_volume_pln"), f("risk") or "low",
                 today, today, app_id))
    con.execute("INSERT INTO accounts VALUES (?, ?, ?, 'PLN', ?, 'active', ?, 0, 0)",
                (account_id, client_id, iban, "business" if applicant_type == "company" else "current", today))
    con.execute("UPDATE onboarding_applications SET status = 'approved', decided_at = ? WHERE application_id = ?",
                (ctx.now.isoformat(), app_id))
    return {"client_id": client_id, "account_id": account_id, "iban": iban}


@tool("request_more_docs", {"app_id": (str, True), "reason": (str, True)}, "write", "Ask the applicant for more documents.")
def request_more_docs(con, ctx, app_id, reason):
    return _set_status(con, ctx, app_id, "more_docs_requested")


@tool("escalate_edd", {"app_id": (str, True), "reason": (str, True)}, "write", "Escalate the application to enhanced due diligence.")
def escalate_edd(con, ctx, app_id, reason):
    return _set_status(con, ctx, app_id, "edd")


@tool("reject_application", {"app_id": (str, True), "reason": (str, True)}, "write", "Reject the application.")
def reject_application(con, ctx, app_id, reason):
    return _set_status(con, ctx, app_id, "rejected")
