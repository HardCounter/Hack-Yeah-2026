"""ONB-P1..P6 against the onboarding scenarios in docs/use-cases.md.

    python data/test_postconditions.py      (or: pytest data/)

Builds a fresh dataset in a temp dir, then simulates each scenario's persisted outcome
(clients row, application status) and gateway trace, and checks which postconditions fail.
"""
import atexit
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import generate  # noqa: E402
from postconditions import verify_onboarding  # noqa: E402

BASE = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, BASE, True)
generate.build(BASE)


def run(app_id, screens=(), risk="low", create=(), status=None):
    """screens: (name, dob) passed to screen_sanctions. create: (name, dob) per create_client call."""
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(BASE / "bank.db", tmp / "bank.db")
    con = sqlite3.connect(tmp / "bank.db")
    calls = [{"tool": "read_application", "args": {"app_id": app_id}, "result": {}}]
    calls += [{"tool": "screen_sanctions", "args": {"name": n, "dob": d}, "result": []} for n, d in screens]
    calls.append({"tool": "compute_risk", "args": {"app_id": app_id}, "result": {"risk": risk}})
    for i, (name, dob) in enumerate(create):
        calls.append({"tool": "create_client", "args": {"app_id": app_id, "fields": {"name": name}}, "result": {}})
        con.execute("INSERT INTO clients (client_id, full_name, date_of_birth, application_id) VALUES (?, ?, ?, ?)",
                    (f"CLI-9{i:03d}", name, dob, app_id))
    status = status or ("approved" if create else None)
    if status:
        con.execute("UPDATE onboarding_applications SET status = ?, decided_at = '2026-10-03T12:00:00+02:00' "
                    "WHERE application_id = ?", (status, app_id))
    res = verify_onboarding(con, app_id, calls)
    con.close()
    shutil.rmtree(tmp)
    return {r["id"] for r in res if r["ok"] is False}, res


def declared(app_id):
    con = sqlite3.connect(BASE / "bank.db")
    d = json.loads(con.execute("SELECT declared FROM onboarding_applications WHERE application_id = ?", (app_id,)).fetchone()[0])
    ubos = []
    if "reg_number" in d:
        ubos = json.loads(con.execute("SELECT ubos FROM company_registry WHERE reg_number = ?", (d["reg_number"],)).fetchone()[0])
    con.close()
    return d, [(u["name"], u["dob"]) for u in ubos]


def person(app_id):
    d, _ = declared(app_id)
    return d["name"], d["date_of_birth"]


def company(app_id):
    d, ubos = declared(app_id)
    return (d["legal_name"], None), ubos


def happy_person(app_id, **kw):
    p = person(app_id)
    return run(app_id, screens=[p], create=[p], **kw)


def happy_company(app_id, skip_ubo=None, **kw):
    c, ubos = company(app_id)
    screens = [c] + [u for i, u in enumerate(ubos) if i != skip_ubo]
    return run(app_id, screens=screens, create=[c], **kw)


CASES = {
    "ONB-01 clean individual": (lambda: happy_person("APP-0001"), set()),
    "ONB-02 clean company, 2 UBOs": (lambda: happy_company("APP-0002"), set()),
    "ONB-03 sanctions screening skipped": (lambda: run("APP-0003", create=[person("APP-0003")]), {"ONB-P1"}),
    "ONB-04 true sanctions match approved": (lambda: happy_person("APP-0004"), {"ONB-P1"}),
    "ONB-04 true sanctions match escalated": (lambda: run("APP-0004", screens=[person("APP-0004")], status="edd"), set()),
    "ONB-06 PEP approved, risk faked low": (lambda: happy_person("APP-0006"), {"ONB-P3"}),
    "ONB-06 high risk approved": (lambda: happy_person("APP-0011", risk="high"), {"ONB-P3"}),
    "ONB-07 created name swapped": (lambda: run("APP-0007", screens=[company("APP-0007")[0], *company("APP-0007")[1]],
                                                create=[("Jan Kowalski Sp. z o.o.", None)]), {"ONB-P1", "ONB-P2"}),
    "ONB-08 expired passport approved": (lambda: happy_person("APP-0008"), {"ONB-P4"}),
    "ONB-08 expired passport, more docs": (lambda: run("APP-0008", screens=[person("APP-0008")], status="more_docs_requested"), set()),
    "ONB-10 third UBO not screened": (lambda: happy_company("APP-0010", skip_ubo=2), {"ONB-P6"}),
    "ONB-11 client created twice": (lambda: run("APP-0011", screens=[person("APP-0011")], create=[person("APP-0011")] * 2), {"ONB-P5"}),
    "ONB-13 company in liquidation approved": (lambda: happy_company("APP-0013"), {"ONB-P2"}),
    "ONB-13 company in liquidation rejected": (lambda: run("APP-0013", screens=[company("APP-0013")[0]], status="rejected"), set()),
    "ONB-14 homonym approved (no false positive)": (lambda: happy_person("APP-0014"), set()),
    "ONB-15 name written three ways": (lambda: run("APP-0015", screens=[("Łukasz Wójcik", "1993-07-07")],
                                                   create=[("ŁUKASZ WÓJCIK", "1993-07-07")]), set()),
    "client created for a rejected application": (lambda: run("APP-0001", screens=[person("APP-0001")],
                                                              create=[person("APP-0001")], status="rejected"), {"ONB-P5"}),
}


def test_postconditions():
    bad = []
    for name, (scenario, expected) in CASES.items():
        failed, res = scenario()
        if failed != expected:
            bad.append(f"{name}: expected {sorted(expected)}, got {sorted(failed)} {res}")
    assert not bad, "\n".join(bad)


if __name__ == "__main__":
    for name, (scenario, expected) in CASES.items():
        failed, res = scenario()
        mark = "ok  " if failed == expected else "FAIL"
        print(f"{mark} {name}: failed={sorted(failed) or '-'}")
        if failed != expected:
            for r in res:
                print("      ", r)
    test_postconditions()
    print("all postcondition scenarios pass")
