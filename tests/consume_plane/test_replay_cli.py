"""End to end: drop-in plugin + YAML config + JSONL replay through `python -m consume_plane`."""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_replay_runs_velocity_guard_and_exports_findings(tmp_path):
    config = tmp_path / "consume_plane.yaml"
    config.write_text(textwrap.dedent(f"""
        consume_plane:
          source: {{poll_timeout_s: 0.01}}
          plugin_dirs: ["{REPO / 'plugins'}"]
          ledger_path: "var/ledger.db"
          sinks:
            - {{type: jsonl, path: "runs/{{run_id}}/findings.jsonl"}}
          feedback:
            allowed_actions: {{velocity-guard: [REQUIRE_APPROVAL_FOR]}}
        plugins:
          velocity-guard:
            config: {{window_s: 10, max_calls: 8}}
    """))
    proc = subprocess.run(
        [sys.executable, "-m", "consume_plane", "--config", str(config),
         "--replay", str(FIXTURES / "velocity_burst.jsonl"),
         "--contracts", str(FIXTURES / "velocity_burst.contracts.jsonl")],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["plugins"] == ["velocity-guard@1.0.0"]
    assert summary["events_acked"] == 12 and summary["plugin_dead_letters"] == []
    # calls 9 and 10 exceed the limit; the second proposal merges into the first
    assert [f["outcome"] for f in summary["feedback"]] == ["accepted", "already_active"]

    findings = [json.loads(line) for line in (tmp_path / "runs/run_demo/findings.jsonl").read_text().splitlines()]
    assert [f["trigger_event_id"] for f in findings] == ["evt_sess_demo_9", "evt_sess_demo_10"]
    assert all(f["rule_id"] == "velocity.tool_calls" and f["plugin_version"] == "1.0.0" for f in findings)
    assert len(findings[1]["evidence_event_ids"]) == 10


def test_bad_config_exits_with_error(tmp_path):
    config = tmp_path / "bad.yaml"
    config.write_text("consume_plane: {partitionz: 1}\n")
    proc = subprocess.run([sys.executable, "-m", "consume_plane", "--config", str(config)],
                          cwd=REPO, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and "unknown keys" in proc.stderr
