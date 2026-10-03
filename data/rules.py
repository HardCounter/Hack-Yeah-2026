"""Rules shared by data/generate.py, the tools and the outcome verifier.

- AML monitoring rules R-* (docs/use-cases.md): raise the alerts, re-checked for TXM-P1.
  Transactions are dicts shaped like rows of `transactions`.
- KYC name normalization and sanctions/PEP screening: used by `screen_sanctions` and re-run
  independently by the verifier for ONB-P1/P3/P6.
"""
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

FX_PLN = {"PLN": 1.0, "EUR": 4.30, "USD": 3.95}
HIGH_RISK = {"IR", "KP", "MM", "SY", "YE", "AF", "VE", "HT", "ML", "SS"}  # MOCK list
COUNTRY_ALIASES = {"POL": "PL", "POLAND": "PL", "POLSKA": "PL", "DEU": "DE", "GERMANY": "DE", "NIEMCY": "DE"}
CEST = timezone(timedelta(hours=2))


def when(t):
    return datetime.fromisoformat(t["booked_at"].replace("Z", "+00:00"))


def eur(t):
    return t["amount"] * FX_PLN[t["currency"]] / FX_PLN["EUR"]


def pln(t):
    return t["amount"] * FX_PLN[t["currency"]]


def country(c):
    c = (c or "").strip().upper()
    return COUNTRY_ALIASES.get(c, c)


def dedupe(txs):
    """Drop feed-retry duplicates: same account, reference, amount and direction within 5 s."""
    last, out = {}, []
    for t in sorted(txs, key=when):
        k = (t["account_id"], t["reference"], t["amount"], t["direction"])
        if t["reference"] and k in last and when(t) - last[k] <= timedelta(seconds=5):
            continue
        last[k] = when(t)
        out.append(t)
    return out


def _clusters(txs, span, min_n):
    """Union of every window [t, t + span] that holds at least min_n transactions."""
    hit = set()
    for i, t in enumerate(txs):
        win = [u for u in txs[i:] if when(u) - when(t) <= span]
        if len(win) >= min_n:
            hit.update(u["tx_id"] for u in win)
    return hit


def run_rules(txs, client_of, expected, dedup=False):
    """Return one hit per (rule, account): {rule, account_id, client_id, tx_ids, triggered_at}.

    client_of: account_id -> client_id. expected: client_id -> expected_monthly_volume_pln.
    """
    if dedup:
        txs = dedupe(txs)
    txs = sorted(txs, key=when)
    by_acc = defaultdict(list)
    for t in txs:
        by_acc[t["account_id"]].append(t)

    hits = defaultdict(set)
    for acc, ts in by_acc.items():
        cash = [t for t in ts if t["channel"] == "cash_deposit" and 13500 <= eur(t) < 15000]
        hits["R-STRUCT", acc] |= _clusters(cash, timedelta(days=7), 3)
        hits["R-VELOCITY", acc] |= _clusters([t for t in ts if t["direction"] == "out"], timedelta(hours=24), 31)
        hits["R-HRJ", acc] |= {t["tx_id"] for t in ts if country(t["counterparty_country"]) in HIGH_RISK}
        for i, t in enumerate(ts):
            if t["direction"] != "in":
                continue
            if eur(t) >= 20000:
                outs = [u for u in ts[i + 1:] if u["direction"] == "out" and when(u) - when(t) <= timedelta(hours=48)]
                if sum(map(eur, outs)) >= 0.8 * eur(t):
                    hits["R-RAPID", acc] |= {t["tx_id"], *(u["tx_id"] for u in outs)}
            if eur(t) >= 10000 and i and when(t) - when(ts[i - 1]) >= timedelta(days=180):
                hits["R-DORMANT", acc].add(t["tx_id"])

    # R-PROFILE is per client and month (local time), reported on the client's primary account.
    primary, monthly = {}, defaultdict(list)
    for acc, cli in sorted(client_of.items()):
        primary.setdefault(cli, acc)
    for t in txs:
        if t["direction"] == "in" and t["channel"] != "internal":
            monthly[client_of[t["account_id"]], when(t).astimezone(CEST).strftime("%Y-%m")].append(t)
    for (cli, _), ins in monthly.items():
        if sum(map(pln, ins)) > 3 * expected[cli]:
            hits["R-PROFILE", primary[cli]] |= {t["tx_id"] for t in ins}

    by_id = {t["tx_id"]: t for t in txs}
    out = [{"rule": r, "account_id": a, "client_id": client_of[a], "tx_ids": sorted(ids),
            "triggered_at": max(when(by_id[i]) for i in ids).isoformat()}
           for (r, a), ids in hits.items() if ids]
    return sorted(out, key=lambda h: (h["triggered_at"], h["rule"], h["account_id"]))


# ---------------------------------------------------------------- KYC screening

MATCH_THRESHOLD = 0.85  # a hit at or above this must not be ignored (ONB-P1)
LEGAL_FORMS = {"spolka z ograniczona odpowiedzialnoscia": "sp z o o", "spolka akcyjna": "s a", "spolka komandytowa": "sp k"}


def norm(name):
    """Casefold, strip diacritics and punctuation, shorten Polish legal forms, sort tokens."""
    s = (name or "").replace("ł", "l").replace("Ł", "L")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).casefold()
    s = " ".join(re.sub(r"[^\w]+", " ", s).split())
    for long, short in LEGAL_FORMS.items():
        s = s.replace(long, short)
    return " ".join(sorted(s.split()))


def _dob_factor(dob, listed):
    """Listed DOB may be a full date, a year, or unknown. A contradicting DOB rules the match out."""
    if not dob or not listed:
        return 0.85
    if len(listed) == 4:
        return 0.9 if dob[:4] == listed else 0.4
    return 1.0 if dob == listed else 0.4


def screen(name, dob, entries, floor=0.3):
    """Score one subject against sanctions/PEP rows ({entry_id, primary_name, aliases, date_of_birth}).

    score = best name similarity over primary name + aliases * DOB factor. Nationality is ignored on
    purpose (dual citizenship, outdated list data). Returns candidates >= floor, best first.
    """
    n = norm(name)
    hits = []
    for e in entries:
        names = [e["primary_name"], *e["aliases"]]
        sim, matched = max((SequenceMatcher(None, n, norm(x)).ratio(), x) for x in names)
        score = round(sim * _dob_factor(dob, e["date_of_birth"]), 3)
        if score >= floor:
            hits.append({"entry_id": e["entry_id"], "matched_name": matched, "score": score})
    return sorted(hits, key=lambda h: -h["score"])
