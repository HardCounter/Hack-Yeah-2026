"""Report the guardrail suite to the dashboard.

Every case keeps a trace: one step per decision the control layer made, in order. When
SUITE_REPORT names a file (web/suite.py sets it), append one JSON line per collected case and
one per finished case with its trace; the case's status is the last decision in the trace.
SUITE_CASE narrows the run to the one case with that id.
"""
import json
import os

import pytest

REPORT = os.environ.get("SUITE_REPORT")
ONLY = os.environ.get("SUITE_CASE")


def _emit(row):
    with open(REPORT, "a", encoding="utf-8") as report:
        report.write(json.dumps(row) + "\n")


@pytest.fixture(autouse=True)
def suite_trace(request, monkeypatch):
    """The decisions made during this case. Tool calls and prompts through the governed runtime
    are recorded here; a case that reaches the control layer another way appends its own steps."""
    steps = []
    request.node.user_properties.append(("steps", steps))
    try:
        from simulation.governed import GovernedRuntime
    except ImportError:  # a suite that does not use the governed runtime
        return steps

    def traced(method, kind):
        def call(self, name, *args, **kwargs):
            seen = len(self.decisions)
            try:
                return method(self, name, *args, **kwargs)
            finally:
                steps.extend({"kind": kind, "name": name, "decision": made.decision, "reason": made.reason_code}
                             for made in self.decisions[seen:])
        return call

    monkeypatch.setattr(GovernedRuntime, "execute", traced(GovernedRuntime.execute, "tool"))
    monkeypatch.setattr(GovernedRuntime, "prompt", traced(GovernedRuntime.prompt, "prompt"))
    return steps


def pytest_collection_modifyitems(config, items):
    if ONLY:
        dropped = [item for item in items if item.nodeid != ONLY]
        items[:] = [item for item in items if item.nodeid == ONLY]
        config.hook.pytest_deselected(items=dropped)


def pytest_collection_finish(session):
    if not REPORT:
        return
    for item in session.items:
        area = item.get_closest_marker("area")
        _emit({"event": "case", "id": item.nodeid, "area": area.args[0] if area else "OTHER",
               "title": item.name.removeprefix("test_").replace("_", " ")})


def pytest_runtest_logreport(report):
    if not REPORT or not (report.when == "call" or (report.when == "setup" and report.outcome != "passed")):
        return
    steps = dict(report.user_properties).get("steps") or []
    _emit({"event": "result", "id": report.nodeid, "ms": round(report.duration * 1000), "steps": steps,
           "status": steps[-1]["decision"] if steps else None})
