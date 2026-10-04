"""Generate actual synthetic gateway evidence and serve the read API without a model.

uv run --locked python -m persistence.http_api.demo --port 8790
Creates a fresh isolated demo directory, never rewrites an existing bank or config.
"""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

import uvicorn

from persistence.http_api.app import create_app


def build_demo(parent):
    from simulation import agent
    import generate
    parent = Path(parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="rest-demo-", dir=parent))
    generate.build(root / "dataset")
    evidence = root / "bank-runs"
    evidence.mkdir()
    allowed_bank = evidence / "allowed.bank.db"
    blocked_bank = evidence / "blocked.bank.db"
    shutil.copyfile(root / "dataset" / "bank.db", allowed_bank)
    shutil.copyfile(root / "dataset" / "bank.db", blocked_bank)
    # Reuse the existing trusted scripted workflow and its approved baseline policy.
    allowed = agent.run("APP-0001", db=allowed_bank, quiet=True)
    blocked = agent.Session("APP-0001", db=blocked_bank, quiet=True, label="blocked")
    try:
        blocked.execute("read_application", {"app_id": "APP-0001"})
        blocked.execute("check_registry", {"reg_number": "AX2344"})
        blocked.runtime.finish()
    finally:
        blocked.runtime.close()
    manifest = {"evidence_dir": str(evidence), "allowed_session": allowed["session_id"],
                "blocked_session": blocked.id}
    (root / "demo.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-dir", type=Path, default=Path("var/rest-demos"))
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    demo = build_demo(args.runs_dir)
    print(json.dumps(demo, indent=2), flush=True)
    base = f"http://127.0.0.1:{args.port}/api/v1"
    print(f"Real SQLite evidence: {demo['evidence_dir']}\n"
          f"curl {base}/sessions\n"
          f"curl {base}/trajectories/session/{demo['blocked_session']}\n"
          f"curl {base}/sessions/{demo['allowed_session']}/verification\n"
          f"curl -OJ {base}/export/sessions/{demo['allowed_session']}", flush=True)
    app = create_app(evidence_dir=Path(demo["evidence_dir"]))
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
