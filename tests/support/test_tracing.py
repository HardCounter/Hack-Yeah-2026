"""Pipeline trace logging: the three logger implementations and the end-to-end trace."""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import tracing
from tracing import FileLogger, NullLogger, TerminalLogger, configure, get_logger, make_logger, set_logger

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def restore_logger():
    previous = set_logger(NullLogger())
    yield
    set_logger(previous)


def test_null_logger_ignores_everything():
    NullLogger().log("intercept", "action.received", event="evt_1", session="s")  # no error, no output


def test_terminal_logger_writes_one_line_and_never_serializes_structures():
    out = io.StringIO()
    TerminalLogger(out, color=False).log("intercept", "action.decided", session="ses_1", tool="read",
                                         decision="BLOCK", reason=None, arguments={"pesel": "44051401359"})
    line = out.getvalue()
    assert line.count("\n") == 1
    assert "intercept" in line and "action.decided" in line and "decision=BLOCK" in line
    assert "reason=" not in line                    # None fields are dropped
    assert "44051401359" not in line and "arguments=<dict>" in line


def test_file_logger_appends_json_lines(tmp_path):
    path = tmp_path / "nested" / "trace.log"
    logger = FileLogger(path)
    logger.log("persistence", "evidence.committed", event_id="evt_1", seq=3, text="é" * 500)
    logger.log("consume", "event.processed", event="evt_1", outcome="acked")  # reserved key: renamed
    assert "…".encode("utf-8") in path.read_bytes()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(r["layer"], r["event"]) for r in rows] == [("persistence", "evidence.committed"), ("consume", "event.processed")]
    assert rows[0]["seq"] == 3 and len(rows[0]["text"]) <= tracing.MAX_VALUE_CHARS + 1
    assert rows[0]["ts"].endswith("+00:00")
    assert rows[1]["event"] == "event.processed" and rows[1]["event_"] == "evt_1"


def test_selection_by_name_and_environment(tmp_path, monkeypatch):
    assert isinstance(make_logger("terminal"), TerminalLogger)
    assert isinstance(make_logger("null"), NullLogger)
    assert isinstance(make_logger("file", tmp_path / "t.log"), FileLogger)
    with pytest.raises(ValueError):
        make_logger("syslog")
    monkeypatch.setenv("CONTROL_LOG", "file")
    monkeypatch.setenv("CONTROL_LOG_FILE", str(tmp_path / "env.log"))
    set_logger(None)
    assert isinstance(get_logger(), FileLogger) and get_logger().path == tmp_path / "env.log"
    assert isinstance(configure("null"), NullLogger) and isinstance(get_logger(), NullLogger)


@pytest.mark.skipif(shutil.which("uv") is None and not (ROOT / "data" / "bank.db").exists(), reason="needs data")
def test_governed_run_traces_all_three_layers(tmp_path):
    if not (ROOT / "data" / "bank.db").exists():
        subprocess.run([sys.executable, "data/generate.py"], cwd=ROOT, check=True, capture_output=True)
    trace = tmp_path / "trace.log"
    env = {**os.environ, "CONTROL_LOG": "file", "CONTROL_LOG_FILE": str(trace), "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([sys.executable, "-m", "simulation.agent", "APP-0003", "--driver", "scripted",
                          "--fault", "skip_step:screen_sanctions"],
                          cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert done.returncode == 1  # VERIFICATION_INCOMPLETE, as documented
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    events = [(r["layer"], r["event"]) for r in rows]

    # the session-start evidence is committed first, then the session is announced, then actions follow
    assert events.index(("intercept", "session.started")) < events.index(("intercept", "action.received"))
    for expected in [("intercept", "action.received"), ("intercept", "action.decided"),
                     ("persistence", "evidence.committed"), ("consume", "event.processed"),
                     ("consume", "finding"), ("consume", "verification"), ("intercept", "session.finished")]:
        assert expected in events, expected
    # every decided tool call follows its interception, and its evidence is committed with a seq
    decided = [r for r in rows if r["event"] == "action.decided"]
    received = {r["action"] for r in rows if r["event"] == "action.received"}
    committed = {r["action"] for r in rows if r["event"] == "evidence.committed" and r.get("seq") is not None}
    assert decided and all(r["action"] in received and r["action"] in committed for r in decided)
    assert all(r["outcome"] == "acked" for r in rows if r["event"] == "event.processed")
    [verification] = [r for r in rows if r["event"] == "verification"]
    assert verification["status"] == "VERIFICATION_INCOMPLETE"
    # trace values are identifiers and codes only: no raw applicant data
    with sqlite3.connect(ROOT / "data" / "bank.db") as db:
        declared_name = json.loads(db.execute(
            "select declared from onboarding_applications where application_id='APP-0003'"
        ).fetchone()[0])["name"]
    assert declared_name not in trace.read_text(encoding="utf-8")
