"""Local model responses use the same persisted governance path as scripts."""
import json
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "simulation"))
import agent
import generate


@pytest.fixture
def session(tmp_path):
    generate.build(tmp_path / "dataset")
    db = tmp_path / "bank.db"
    shutil.copy(tmp_path / "dataset" / "bank.db", db)
    session = agent.Session("APP-0001", db=db, quiet=True)
    yield session
    session.runtime.close()


def test_llm_driver_persists_model_calls_and_prevents_unscreened_write(session, monkeypatch):
    requests = []

    def backend(model, messages, tools, max_tokens):
        requests.append((model, max_tokens))
        name = "read_application" if len(requests) == 1 else "create_client"
        args = {"app_id": session.app_id}
        if name == "create_client":
            args["fields"] = {"name": "Unverified Person", "dob": "1990-01-01"}
        return {"tool_calls": [{"id": f"call_{len(requests)}", "function": {
            "name": name, "arguments": json.dumps(args)}}]}

    monkeypatch.setattr(agent, "_chat", backend)
    agent.llm(session, max_calls=2)
    assert len(requests) == 2
    events = session.runtime.events()
    assert len([e for e in events if e["action_type"] == "llm_call"]) == 2
    tools = [e for e in events if e["action_type"] == "tool_call"]
    assert [e["action_details"]["name"] for e in tools] == ["read_application", "create_client"]
    assert [e["status"] for e in tools] == ["completed", "blocked"]
    assert session.runtime.decisions[-1].decision == "BLOCK"
    with sqlite3.connect(session.ctx.db) as con:
        assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id=?", (session.app_id,)).fetchone()[0] == 0
    assert "Unverified Person" not in json.dumps(events)
    assert session.runtime.runner.run(session.runtime.store.pending_deliveries()) == 0
    assert session.runtime.finish().verification_status != "VERIFIED_SUCCESS"


def test_llm_driver_model_deny_never_dispatches(session, monkeypatch):
    def backend(*args, **kwargs):
        pytest.fail("unapproved model must not execute")

    monkeypatch.setattr(agent, "_chat", backend)
    agent.llm(session, model="unapproved-model")
    assert session.n == 0
    event = session.runtime.events()[-1]
    assert event["action_type"] == "llm_call" and event["status"] == "blocked"
    assert session.runtime.decisions[-1].reason_code == "MODEL_NOT_AUTHORIZED"
