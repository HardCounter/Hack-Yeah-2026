"""AML monitoring rules R-* from docs/use-cases.md.

Used by data/generate.py to raise the alerts, and by the outcome verifier to re-check an
agent's alert disposition (TXM-P1). Transactions are dicts shaped like rows of `transactions`.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

FX_PLN = {"PLN": 1.0, "EUR": 4.30, "USD": 3.95}
HIGH_RISK = {"IR", "KP", "MM", "SY", "YE", "AF", "VE", "HT", "ML", "SS"}  # MOCK list
COUNTRY_ALIASES = {"POL": "PL", "POLAND": "PL", "POLSKA": "PL", "DEU": "DE", "GERMANY": "DE", "NIEMCY": "DE"}
CEST = timezone(timedelta(hours=2))


def when(t):
    return datetime.fromisoformat(t["booked_at"])


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
