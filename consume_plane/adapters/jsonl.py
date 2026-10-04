"""JSONL adapters: replay a recorded run, and export findings as audit evidence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from contracts.task_contract import TaskContract
from contracts.wire import DecodeError, decode_event
from ..model.outputs import Finding, PluginDecision
from .memory import MemoryEventSource, MemoryTrajectoryReader


def read_jsonl(path: str | Path) -> list[tuple[int, dict[str, Any]]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if line.strip():
                rows.append((lineno, json.loads(line)))
    return rows


def _decode_file(path: str | Path):
    actions, errors = [], []
    for lineno, raw in read_jsonl(path):
        try:
            actions.append(decode_event(raw))
        except DecodeError as e:
            errors.append(f"{path}:{lineno}: {e}")
    return actions, errors


class JsonlReplaySource(MemoryEventSource):
    """Feeds a recorded run in file order. Undecodable lines are kept in `decode_errors`, not delivered."""

    def __init__(self, path: str | Path, **kwargs):
        actions, self.decode_errors = _decode_file(path)
        super().__init__(actions, **kwargs)


def jsonl_trajectory_reader(events_path: str | Path, contracts_path: str | Path | None = None) -> MemoryTrajectoryReader:
    actions, _ = _decode_file(events_path)
    contracts = [TaskContract.from_dict(raw) for _, raw in read_jsonl(contracts_path)] if contracts_path else []
    return MemoryTrajectoryReader(actions, contracts)


class JsonlSink:
    """Appends findings to a file; `{run_id}` in the path is filled per finding."""

    def __init__(self, path_template: str | Path):
        self.path_template = str(path_template)

    async def write(self, findings: Sequence[Finding]) -> None:
        for f in findings:
            path = Path(self.path_template.format(run_id=f.run_id or "unknown"))
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as out:
                out.write(json.dumps(f.to_dict(), sort_keys=True) + "\n")

    async def write_decisions(self, decisions: Sequence[PluginDecision]) -> None:
        """Decision trace next to the findings file: findings.jsonl -> decisions.jsonl."""
        for d in decisions:
            path = Path(self.path_template.format(run_id=d.run_id or "unknown"))
            path = path.with_name("decisions.jsonl" if path.name == "findings.jsonl" else path.stem + ".decisions.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as out:
                out.write(json.dumps(d.to_dict(), sort_keys=True) + "\n")
