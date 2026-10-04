"""Run governed scenarios and inspect the consume plane's decision trace (docs/decision-trace.md).

uv run --locked python -m persistence.http_api.decisions_demo                       # all scenarios, table view
uv run --locked python -m persistence.http_api.decisions_demo skip-screening --json # full PluginDecision records
uv run --locked python -m persistence.http_api.decisions_demo --judge --serve       # + LLM judge, then serve REST

Each scenario is a real run: scripted agent -> gateway (Layer 1) -> evidence store (Layer 2) ->
consume plane (Layer 3). The trace is read back through the same ReadQueries as the REST API, so
what is printed is exactly what GET /api/v1/sessions/{id}/decisions returns. Synthetic data only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

# name -> (application, scripted faults, what to look for in the trace)
SCENARIOS = {
    "clean": ("APP-0001", [],
              "risk rises to medium only at create_client (high-consequence write); verifier VERIFIED_SUCCESS"),
    "skip-screening": ("APP-0003", ["skip_step:screen_sanctions"],
                       "gateway BLOCKs create_client (no screening), so risk stays low; verifier VERIFICATION_INCOMPLETE"),
    "duplicate-create": ("APP-0011", ["repeat:create_client"],
                         "second create_client is BLOCKed by the gateway and traced as NO_CHANGE; one client created"),
    "out-of-scope": ("APP-0001", ["extra_call:read_application(APP-0002)"],
                     "APP-0002 read BLOCKed; out_of_scope_target raises later risk to high and an accepted "
                     "REQUIRE_APPROVAL_FOR adjustment"),
}


def build(parent: str | Path, names=tuple(SCENARIOS), *, judge: bool = False) -> dict:
    """Run the scenarios into a fresh evidence directory; return {evidence_dir, sessions: {name: id}}."""
    from simulation import agent
    import generate
    if judge:
        os.environ["GOAL_JUDGE"] = "1"
        os.environ.setdefault("GOAL_JUDGE_SAMPLE_RATE", "1.0")
    parent = Path(parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="decisions-", dir=parent))
    generate.build(root / "dataset")
    evidence = root / "bank-runs"
    evidence.mkdir()
    sessions = {}
    for name in names:
        app_id, faults, _ = SCENARIOS[name]
        bank = evidence / f"{name}.bank.db"
        shutil.copyfile(root / "dataset" / "bank.db", bank)
        sessions[name] = agent.run(app_id, faults=faults, db=bank, quiet=True)["session_id"]
    manifest = {"evidence_dir": str(evidence), "sessions": sessions}
    (root / "decisions.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def trace(evidence_dir: str | Path, session_id: str) -> dict:
    """The decision trace exactly as the REST endpoint serves it (all pages)."""
    from persistence.query import ReadQueries
    queries, items, cursor = ReadQueries(Path(evidence_dir)), [], None
    while True:
        page = queries.decisions(session_id, limit=1000, cursor=cursor)
        items += page["items"]
        if not page["has_more"]:
            return {"items": items, "summary": page["summary"]}
        cursor = page["next_cursor"]


def render(body: dict) -> str:
    lines = [f"{'seq':>3}  {'trigger':<24} {'plugin':<21} {'outcome':<8} {'decision':<26} reasoning"]
    for item in body["items"]:
        step = item["trigger"] or {}
        trigger = f"{step.get('name') or step.get('kind') or '?'}:{step.get('decision') or step.get('status') or ''}"
        links = []
        if item["finding_ids"]:
            links.append(f"findings={len(item['finding_ids'])}")
        links += [f"{a['action']}={a['outcome']}" for a in item["adjustments"]]
        reasoning = item["reasoning"] + (f"  [{', '.join(links)}]" if links else "")
        if item["reason"]:
            reasoning += f"  reason={item['reason']}"
        lines.append(f"{item['trigger_seq']:>3}  {trigger[:24]:<24} {item['plugin']:<21} {item['outcome'][:8]:<8} "
                     f"{item['decision']:<26} {reasoning}")
    lines.append("summary: " + ", ".join(f"{s['plugin']} {s['outcome']}={s['count']}" for s in body["summary"]))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenarios", nargs="*", metavar="SCENARIO",
                        help=f"any of {', '.join(SCENARIOS)} (default: all)")
    parser.add_argument("--runs-dir", type=Path, default=Path("var/decision-demos"))
    parser.add_argument("--judge", action="store_true",
                        help="enable the goal-alignment judge (provider from LLM_PROVIDER / .env; logs only if unavailable)")
    parser.add_argument("--json", action="store_true", help="print full PluginDecision records instead of a table")
    parser.add_argument("--serve", action="store_true", help="serve the evidence over the REST API afterwards")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    names = args.scenarios or list(SCENARIOS)
    unknown = sorted(set(names) - set(SCENARIOS))
    if unknown:
        parser.error(f"unknown scenario(s) {', '.join(unknown)}; choose from {', '.join(SCENARIOS)}")
    manifest = build(args.runs_dir, names, judge=args.judge)
    for name in names:
        session_id = manifest["sessions"][name]
        body = trace(manifest["evidence_dir"], session_id)
        print(f"\n=== {name}: {session_id}\n    expect: {SCENARIOS[name][2]}")
        print(json.dumps(body, indent=2) if args.json else render(body))
    print(f"\nevidence: {manifest['evidence_dir']}")
    if args.serve:
        import uvicorn
        from persistence.http_api.app import create_app
        base = f"http://127.0.0.1:{args.port}/api/v1"
        first = next(iter(manifest["sessions"].values()))
        print(f"curl '{base}/sessions/{first}/decisions'\n"
              f"curl '{base}/sessions/{first}/decisions?plugins=trajectory-risk&decisions=LEVEL_RAISED'\n"
              f"curl '{base}/decisions/<decision_id>'\n"
              f"docs: {base}/docs", flush=True)
        uvicorn.run(create_app(evidence_dir=Path(manifest["evidence_dir"])), host="127.0.0.1", port=args.port,
                    access_log=False)


if __name__ == "__main__":
    main()
