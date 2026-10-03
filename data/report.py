"""Render data/report.html: a one-page explorer of bank.db + ground_truth.json.

    python data/generate.py && python data/report.py

Opens offline in any browser. Shows alerts and applications with their ground-truth label, the
scenarios (docs/use-cases.md) that use them, their transactions/documents, and data-quality noise.
"""
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
USE_CASES = HERE.parent / "docs" / "use-cases.md"


def load():
    con = sqlite3.connect(HERE / "bank.db")
    con.row_factory = sqlite3.Row
    q = lambda s: [dict(r) for r in con.execute(s)]
    gt = json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8"))

    scenarios = defaultdict(list)  # data id -> ["ONB-03: ...", ...]
    for line in USE_CASES.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\| ((?:ONB|TXM)-\d+) \|(.*)", line)
        if m:
            cells = [c.strip() for c in m.group(2).split("|")]
            for ref in set(re.findall(r"(?:APP|ALR)-\d{4}", m.group(2))):
                scenarios[ref].append(f"{m.group(1)}: {cells[1]} → {cells[3]}")

    clients = {c["client_id"]: c for c in q("SELECT * FROM clients")}
    accounts = {a["account_id"]: dict(a, client_name=clients[a["client_id"]]["full_name"]) for a in q("SELECT * FROM accounts")}
    tx = q("SELECT tx_id, account_id, booked_at, amount, currency, direction, channel, counterparty_name, "
           "counterparty_country, memo FROM transactions ORDER BY booked_at")
    alerts = []
    for a in q("SELECT * FROM alerts ORDER BY alert_id"):
        g = gt["alerts"][a["alert_id"]]
        c = clients[a["client_id"]]
        alerts.append(dict(id=a["alert_id"], rule=a["rule_id"], client=a["client_id"], name=c["full_name"],
                           account=a["account_id"], triggered=a["triggered_at"][:16], tx_ids=json.loads(a["tx_ids"]),
                           label=g["label"], why=g.get("reason", ""), typology=g.get("typology", ""),
                           kyc=c["kyc_notes"] or "", expected=c["expected_monthly_volume_pln"],
                           scenarios=scenarios.get(a["alert_id"], [])))
    docs = defaultdict(list)
    for d in q("SELECT doc_id, application_id, doc_type, ocr_text FROM documents ORDER BY doc_id"):
        docs[d["application_id"]].append(dict(id=d["doc_id"], type=d["doc_type"], text=d["ocr_text"]))
    apps = []
    for a in q("SELECT * FROM onboarding_applications ORDER BY application_id"):
        decl = json.loads(a["declared"])
        g = gt["applications"][a["application_id"]]
        apps.append(dict(id=a["application_id"], type=a["applicant_type"], name=decl.get("name") or decl.get("legal_name"),
                         label=g["label"], notes=g["notes"], declared=decl, docs=docs[a["application_id"]],
                         scenarios=scenarios.get(a["application_id"], [])))
    daily = Counter(t["booked_at"][:10] for t in tx)
    return dict(
        totals=dict(clients=len(clients), accounts=len(accounts), transactions=len(tx), alerts=len(alerts),
                    suspicious=sum(a["label"] == "suspicious" for a in alerts), applications=len(apps),
                    documents=sum(len(v) for v in docs.values())),
        alerts=alerts, apps=apps, accounts=accounts,
        tx=[[t["tx_id"], t["account_id"], t["booked_at"], t["amount"], t["currency"], t["direction"], t["channel"],
             t["counterparty_name"], t["counterparty_country"], t["memo"]] for t in tx],
        daily=sorted((d, n) for d, n in daily.items() if d >= "2026-07-05"),
        dq={k: len(v) for k, v in gt["dq_issues"].items()} | {"duplicate": len(gt["duplicates"])},
    )


def main():
    data = json.dumps(load(), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out = HERE / "report.html"
    out.write_text(TEMPLATE.replace("__DATA__", data), encoding="utf-8")
    print(f"wrote {out}")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mock Bank Data</title>
<style>
:root{color-scheme:light;--bg:#fcfcfb;--panel:#ffffff;--line:#e4e3df;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8984;
 --s1:#2a78d6;--s2:#eb6834;--hl:#fff4d6;--grid:#ecebe7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1a1a19;--panel:#222220;--line:#383835;
 --ink:#ffffff;--ink2:#c3c2b7;--muted:#8f8e86;--s1:#3987e5;--s2:#d95926;--hl:#3a3220;--grid:#2c2c2a}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1a1a19;--panel:#222220;--line:#383835;--ink:#ffffff;--ink2:#c3c2b7;--muted:#8f8e86;
 --s1:#3987e5;--s2:#d95926;--hl:#3a3220;--grid:#2c2c2a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 12px}p.sub{color:var(--ink2);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.tile span{color:var(--ink2);font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:800px){.grid2{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.card h3{margin:0 0 10px;font-size:14px}
.legend{display:flex;gap:16px;font-size:12px;color:var(--ink2);margin-bottom:8px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.bar-row{display:grid;grid-template-columns:110px 1fr 40px;align-items:center;gap:8px;margin:6px 0;font-size:12px}
.bar-row .lab{color:var(--ink2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bar-row .val{color:var(--ink2);font-variant-numeric:tabular-nums}
.track{display:flex;gap:2px;height:14px}.seg{height:100%;border-radius:0}.seg:first-child{border-radius:4px 0 0 4px}.seg:last-child{border-radius:0 4px 4px 0}.seg:only-child{border-radius:4px}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
select,input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
.tablewrap{overflow:auto;max-height:520px;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{position:sticky;top:0;background:var(--panel);color:var(--ink2);font-weight:600}tbody tr.click{cursor:pointer}tbody tr.click:hover{background:var(--grid)}
tr.sel{background:var(--hl)!important}td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11.5px;border:1px solid var(--line);white-space:nowrap}
.tag.suspicious,.tag.escalate,.tag.reject{border-color:var(--s2);color:var(--ink)}.tag.suspicious::before,.tag.escalate::before,.tag.reject::before{content:"⚠ "}
.detail{margin-top:12px}.detail .meta{color:var(--ink2);margin:4px 0}.detail ul{margin:6px 0;padding-left:18px}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;max-height:320px;overflow:auto}
.tip{position:fixed;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;z-index:9}
svg text{fill:var(--muted);font-size:11px}
</style></head><body><main>
<h1>Mock bank data</h1>
<p class="sub">Generated by <code>data/generate.py</code>. Labels come from <code>ground_truth.json</code>; the agents never see them.</p>
<div class="tiles" id="tiles"></div>

<div class="grid2" style="margin-top:16px">
 <div class="card"><h3>Alerts by rule</h3>
  <div class="legend"><span><i class="sw" style="background:var(--s1)"></i>False positive</span><span><i class="sw" style="background:var(--s2)"></i>Suspicious</span></div>
  <div id="byrule"></div></div>
 <div class="card"><h3>Planted data-quality issues (rows affected)</h3><div id="dq"></div></div>
</div>
<div class="card" style="margin-top:16px"><h3>Transactions per day</h3><div id="daily"></div></div>

<h2>Alerts</h2>
<div class="controls"><select id="fRule"><option value="">All rules</option></select>
 <select id="fLabel"><option value="">All labels</option><option>false_positive</option><option>suspicious</option></select>
 <label><input type="checkbox" id="fScen"> Used in a scenario</label></div>
<div class="tablewrap"><table><thead><tr><th>Alert</th><th>Rule</th><th>Client</th><th>Account</th><th class="num">Tx</th><th>Label</th><th>Why (ground truth)</th></tr></thead><tbody id="alerts"></tbody></table></div>
<div class="detail card" id="alertDetail" hidden></div>

<h2>Onboarding applications</h2>
<div class="tablewrap"><table><thead><tr><th>Application</th><th>Type</th><th>Name (declared)</th><th>Label</th><th>Notes (ground truth)</th><th>Scenarios</th></tr></thead><tbody id="apps"></tbody></table></div>
<div class="detail card" id="appDetail" hidden></div>

<h2>Account explorer</h2>
<div class="controls"><input id="acc" placeholder="ACC-00104" size="12"><input id="q" placeholder="search memo / counterparty" size="28"></div>
<div id="accMeta" class="meta"></div>
<div class="tablewrap"><table><thead><tr><th>Tx</th><th>Booked</th><th class="num">Amount</th><th>Dir</th><th>Channel</th><th>Counterparty</th><th>Ctry</th><th>Memo</th></tr></thead><tbody id="txs"></tbody></table></div>
</main><div class="tip" id="tip"></div>
<script>
const D = __DATA__;
const $ = id => document.getElementById(id), esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = n => n.toLocaleString("en-US", {maximumFractionDigits: 2});
const tip = $("tip");
function showTip(e, html){tip.innerHTML = html; tip.style.display = "block"; tip.style.left = Math.min(e.clientX + 12, innerWidth - 220) + "px"; tip.style.top = (e.clientY + 12) + "px";}
function hideTip(){tip.style.display = "none";}
function hover(el, html){el.addEventListener("mousemove", e => showTip(e, html)); el.addEventListener("mouseleave", hideTip);}

// tiles
const T = D.totals;
$("tiles").innerHTML = [["Clients", T.clients], ["Accounts", T.accounts], ["Transactions", T.transactions],
  ["Alerts", T.alerts], ["Suspicious alerts", T.suspicious], ["Applications", T.applications], ["Documents", T.documents]]
  .map(([l, v]) => `<div class="tile"><b>${fmt(v)}</b><span>${l}</span></div>`).join("");

// alerts by rule: stacked bars
const rules = [...new Set(D.alerts.map(a => a.rule))].sort();
const maxRule = Math.max(...rules.map(r => D.alerts.filter(a => a.rule === r).length));
for (const r of rules) {
  const fp = D.alerts.filter(a => a.rule === r && a.label === "false_positive").length, su = D.alerts.filter(a => a.rule === r && a.label === "suspicious").length;
  const row = document.createElement("div"); row.className = "bar-row";
  row.innerHTML = `<span class="lab">${r}</span><div class="track">${fp ? `<div class="seg" style="width:${fp / maxRule * 100}%;background:var(--s1)"></div>` : ""}${su ? `<div class="seg" style="width:${su / maxRule * 100}%;background:var(--s2)"></div>` : ""}</div><span class="val">${fp + su}</span>`;
  hover(row, `<b>${r}</b><br>False positive: ${fp}<br>Suspicious: ${su}`);
  $("byrule").appendChild(row);
  $("fRule").insertAdjacentHTML("beforeend", `<option>${r}</option>`);
}

// data-quality issues: single-series bars
const dq = Object.entries(D.dq).sort((a, b) => b[1] - a[1]), maxDq = dq[0][1];
for (const [k, v] of dq) {
  const row = document.createElement("div"); row.className = "bar-row";
  row.innerHTML = `<span class="lab" title="${k}">${k.replaceAll("_", " ")}</span><div class="track"><div class="seg" style="width:${v / maxDq * 100}%;background:var(--s1)"></div></div><span class="val">${v}</span>`;
  hover(row, `<b>${k}</b><br>${v} rows`);
  $("dq").appendChild(row);
}

// transactions per day: line with crosshair
(function(){
  const W = 1100, H = 180, P = {l: 36, r: 8, t: 8, b: 22}, pts = D.daily, max = Math.max(...pts.map(p => p[1]));
  const x = i => P.l + i * (W - P.l - P.r) / (pts.length - 1), y = v => H - P.b - v / max * (H - P.t - P.b);
  let g = "";
  for (let k = 0; k <= 4; k++) { const v = Math.round(max * k / 4); g += `<line x1="${P.l}" x2="${W - P.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${P.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`; }
  pts.forEach((p, i) => { if (p[0].endsWith("-01") || i === 0) g += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${p[0].slice(5)}</text>`; });
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  $("daily").innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Transactions per day">${g}<path d="${path}" fill="none" stroke="var(--s1)" stroke-width="2"/><line id="xh" y1="${P.t}" y2="${H - P.b}" stroke="var(--muted)" visibility="hidden"/><circle id="xd" r="4" fill="var(--s1)" stroke="var(--panel)" stroke-width="2" visibility="hidden"/><rect x="${P.l}" y="0" width="${W - P.l - P.r}" height="${H}" fill="transparent" id="hit"/></svg>`;
  const svg = $("daily").firstChild, xh = $("xh"), xd = $("xd");
  $("hit").addEventListener("mousemove", e => {
    const r = svg.getBoundingClientRect(), sx = (e.clientX - r.left) * W / r.width;
    const i = Math.max(0, Math.min(pts.length - 1, Math.round((sx - P.l) / (W - P.l - P.r) * (pts.length - 1))));
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
    xd.setAttribute("cx", x(i)); xd.setAttribute("cy", y(pts[i][1])); xd.setAttribute("visibility", "visible");
    showTip(e, `<b>${pts[i][0]}</b><br>${pts[i][1]} transactions`);
  });
  $("hit").addEventListener("mouseleave", () => { hideTip(); xh.setAttribute("visibility", "hidden"); xd.setAttribute("visibility", "hidden"); });
})();

// transaction rows
const TX = D.tx, byId = Object.fromEntries(TX.map(t => [t[0], t]));
const txRow = (t, mark) => `<tr class="${mark && mark.has(t[0]) ? "sel" : ""}"><td>${t[0]}</td><td>${esc(t[2].slice(0, 19).replace("T", " "))}${t[2].endsWith("Z") ? " Z" : ""}</td><td class="num">${fmt(t[3])} ${t[4]}</td><td>${t[5]}</td><td>${t[6]}</td><td>${esc(t[7])}</td><td>${esc(t[8])}</td><td>${esc(t[9])}</td></tr>`;
const txTable = (rows, mark) => `<div class="tablewrap" style="max-height:320px"><table><thead><tr><th>Tx</th><th>Booked</th><th class="num">Amount</th><th>Dir</th><th>Channel</th><th>Counterparty</th><th>Ctry</th><th>Memo</th></tr></thead><tbody>${rows.map(t => txRow(t, mark)).join("")}</tbody></table></div>`;
const tag = l => `<span class="tag ${l}">${l.replace("_", " ")}</span>`;
const scen = s => s.length ? `<ul>${s.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div class="meta">Not used by a scenario.</div>`;

// alerts table
function renderAlerts(){
  const r = $("fRule").value, l = $("fLabel").value, s = $("fScen").checked;
  $("alerts").innerHTML = D.alerts.filter(a => (!r || a.rule === r) && (!l || a.label === l) && (!s || a.scenarios.length))
    .map(a => `<tr class="click" data-id="${a.id}"><td>${a.id}</td><td>${a.rule}</td><td>${esc(a.name)}<br><span style="color:var(--muted)">${a.client}</span></td><td>${a.account}</td><td class="num">${a.tx_ids.length}</td><td>${tag(a.label)}</td><td>${esc(a.why)}</td></tr>`).join("");
}
["fRule", "fLabel", "fScen"].forEach(id => $(id).addEventListener("change", renderAlerts));
renderAlerts();
$("alerts").addEventListener("click", e => {
  const tr = e.target.closest("tr"); if (!tr) return;
  document.querySelectorAll("#alerts tr").forEach(x => x.classList.toggle("sel", x === tr));
  const a = D.alerts.find(x => x.id === tr.dataset.id), d = $("alertDetail");
  d.hidden = false;
  d.innerHTML = `<h3>${a.id} · ${a.rule} · ${tag(a.label)}</h3>
    <div class="meta">${esc(a.name)} (${a.client}), account ${a.account}, triggered ${a.triggered}. Declared expected volume ${fmt(a.expected)} PLN / month.</div>
    ${a.kyc ? `<div class="meta">KYC notes: ${esc(a.kyc)}</div>` : ""}
    <div class="meta"><b>Scenarios</b></div>${scen(a.scenarios)}
    <div class="meta"><b>Triggering transactions</b> (${a.tx_ids.length})</div>${txTable(a.tx_ids.map(i => byId[i]))}
    <div class="meta"><a href="#acc" onclick="showAccount('${a.account}', new Set(${JSON.stringify(a.tx_ids)}))">Open the whole account in the explorer ↓</a></div>`;
});

// applications
$("apps").innerHTML = D.apps.map(a => `<tr class="click" data-id="${a.id}"><td>${a.id}</td><td>${a.type}</td><td>${esc(a.name)}</td><td>${tag(a.label)}</td><td>${esc(a.notes)}</td><td>${a.scenarios.map(s => s.split(":")[0]).join(", ")}</td></tr>`).join("");
$("apps").addEventListener("click", e => {
  const tr = e.target.closest("tr"); if (!tr) return;
  document.querySelectorAll("#apps tr").forEach(x => x.classList.toggle("sel", x === tr));
  const a = D.apps.find(x => x.id === tr.dataset.id), d = $("appDetail");
  d.hidden = false;
  d.innerHTML = `<h3>${a.id} · ${esc(a.name)} · ${tag(a.label)}</h3><div class="meta">${esc(a.notes)}</div>
    <div class="meta"><b>Scenarios</b></div>${scen(a.scenarios)}
    <div class="meta"><b>Declared on the form</b></div><pre>${esc(JSON.stringify(a.declared, null, 2))}</pre>
    ${a.docs.map(doc => `<div class="meta"><b>${doc.id}</b> · ${doc.type}</div><pre>${esc(doc.text)}</pre>`).join("")}`;
});

// account explorer
let mark = new Set();
function showAccount(id, m){ $("acc").value = id; mark = m || new Set(); renderAccount(); }
function renderAccount(){
  const id = $("acc").value.trim().toUpperCase(), q = $("q").value.trim().toLowerCase(), a = D.accounts[id];
  if (!a) { $("accMeta").textContent = "Type an account ID (ACC-00001 … ACC-00231)."; $("txs").innerHTML = ""; return; }
  const rows = TX.filter(t => t[1] === id && (!q || `${t[7]} ${t[9]}`.toLowerCase().includes(q)));
  $("accMeta").textContent = `${a.client_name} (${a.client_id}) · ${a.account_type} · ${a.currency} · ${a.status} · balance ${fmt(a.balance)} ${a.currency} · ${rows.length} transactions`;
  $("txs").innerHTML = rows.map(t => txRow(t, mark)).join("");
}
$("acc").addEventListener("input", () => { mark = new Set(); renderAccount(); });
$("q").addEventListener("input", renderAccount);
showAccount("ACC-00104");
</script></body></html>
"""

if __name__ == "__main__":
    main()
