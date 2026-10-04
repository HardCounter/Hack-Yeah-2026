"""Real OpenCode CLI → loopback model → governed tools → durable consumers."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "var" / "opencode-cli" / "node_modules" / ".bin" / "opencode"


def _opencode_bin() -> str | None:
    """OPENCODE_BIN, else the pinned CLI, else `opencode` on PATH; the tests run the one found here."""
    override = os.environ.get("OPENCODE_BIN")
    if override:
        path = Path(override).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    if CLI.is_file() and os.access(CLI, os.X_OK):
        return str(CLI)
    return shutil.which("opencode")


def _opencode_available() -> bool:
    return _opencode_bin() is not None


pytestmark = pytest.mark.skipif(not _opencode_available(), reason="OpenCode CLI unavailable; install pinned CLI or set OPENCODE_BIN")


def _build_synthetic_bank(target: Path) -> Path:
    # Load the repository's deterministic generator without depending on which
    # other test module has already imported a module named `generate`.
    sys.path.insert(0, str(ROOT / "data"))
    sys.path.insert(0, str(ROOT / "simulation" / "tools"))
    spec = importlib.util.spec_from_file_location("opencode_test_data_generator", ROOT / "data" / "generate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.build(target)
    return target / "bank.db"


class _FixtureModel:
    """Small deterministic OpenAI-compatible SSE server; never logs request bodies."""

    def __init__(self, *, fail_without_tools: bool = False):
        self.fail_without_tools = fail_without_tools
        self.calls = 0
        self.tool_steps = 0
        self.tool_catalogs: list[tuple[str, ...]] = []
        self.paths: list[str] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size) or b"{}")
                fixture.calls += 1
                fixture.paths.append(self.path)
                catalog = tuple(
                    item.get("function", {}).get("name", "")
                    for item in payload.get("tools", []) if isinstance(item, dict)
                )
                if catalog:
                    fixture.tool_catalogs.append(catalog)
                scripted = [
                    ("read_application", {"app_id": "APP-0001"}),
                    ("read_documents", {"app_id": "APP-0001"}),
                    ("extract_fields", {"doc_id": "DOC-0001"}),
                    ("screen_sanctions", {"name": "Katarzyna Zielińska", "dob": "1990-05-14"}),
                    ("compute_risk", {"app_id": "APP-0001", "factors": {
                        "pep": False, "sanctions_hit": False, "country": "PL",
                        "applicant_type": "individual", "expected_monthly_volume_pln": 8000,
                    }}),
                    ("create_client", {"app_id": "APP-0001", "fields": {
                        "name": "Katarzyna Zielińska", "dob": "1990-05-14",
                    }}),
                ]
                if fixture.fail_without_tools or not catalog:
                    # Title and summary requests carry no tools; only tool turns advance the workflow.
                    payload_out = {"id": "fixture", "object": "chat.completion.chunk",
                                   "choices": [{"index": 0, "delta": {"content": "Unable to process." if fixture.fail_without_tools else "APP-0001"},
                                                 "finish_reason": "stop"}]}
                elif fixture.tool_steps < len(scripted):
                    fixture.tool_steps += 1
                    name, args = scripted[fixture.tool_steps - 1]
                    payload_out = {"id": "fixture", "object": "chat.completion.chunk",
                                   "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": 0, "id": f"fixture_call_{fixture.tool_steps}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                    }]}, "finish_reason": "tool_calls"}]}
                else:
                    payload_out = {"id": "fixture", "object": "chat.completion.chunk",
                                   "choices": [{"index": 0, "delta": {"content": "Completed."},
                                                 "finish_reason": "stop"}]}
                body = ("data: " + json.dumps(payload_out, ensure_ascii=False) + "\n\n"
                        "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _run_opencode(tmp_path: Path, model: _FixtureModel, *, failure=False):
    bank = _build_synthetic_bank(tmp_path / "seed")
    provider_config = tmp_path / "providers.json"
    provider_config.write_text(json.dumps({"providers": {"fixture": {
        "package": "@opencode/ai/providers/openai-compatible",
        "settings": {"baseURL": model.base_url},
        "env": ["OPENAI_API_KEY"],
        "models": {"kyc-fixture": {
            "name": "Synthetic KYC fixture", "tool_call": True,
            "capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
            "limit": {"context": 32768, "output": 2048},
        }},
    }}}, sort_keys=True), encoding="utf-8")
    runs = tmp_path / "runs"
    env = os.environ.copy()
    env["OPENCODE_BIN"] = _opencode_bin()
    # OpenAI-compatible clients require an API key even for a local fixture;
    # keep this synthetic value process-local and never emit it in diagnostics.
    env["OPENAI_API_KEY"] = "local-fixture-key-never-valid-outside-loopback"
    completed = subprocess.run([
        sys.executable, "-m", "simulation.opencode_runner", "APP-0001",
        "--bank-db", str(bank), "--runs-dir", str(runs),
        # This scripted success fixture requires its original no-approval policy, not the
        # operator's persisted selection (standard deliberately requires create_client approval).
        "--policy", str(ROOT / "simulation" / "policy.json"),
        "--provider-config", str(provider_config), "--model", "fixture/kyc-fixture",
        "--timeout", "120", "--agent", "build",
        "--prompt", "Process APP-0001 using the available KYC tools. Complete the workflow.",
    ], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180, check=False)
    # Deliberately return output only for structural parsing; callers should not
    # attach subprocess output to assertion failures (it can contain synthetic PII).
    bank_runs = next(runs.iterdir()) / "bank-runs"
    bank_copy = next(bank_runs.glob("*.bank.db"))
    session_id = bank_copy.name.removesuffix(".bank.db")
    evidence_db = bank_runs / f"{session_id}.evidence.db"
    ledger_db = evidence_db.with_suffix(".ledger.db")
    return completed, bank_copy, evidence_db, ledger_db, session_id


def _read_verification(path: Path, session_id: str):
    from persistence.store import EventStore

    async def read():
        store = EventStore(str(path))
        await store.initialize()
        try:
            return await store.get_verification(session_id), await store.get_events_by_session_seq(session_id), await store.pending_deliveries()
        finally:
            await store.close()

    return asyncio_run(read())


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def test_real_opencode_cli_runs_governed_kyc_and_drains_consumers(tmp_path):
    from persistence.adapters.consumer_v21 import to_consumer_v21
    from persistence.store import EventStore

    with _FixtureModel() as fixture:
        completed, bank_copy, evidence_db, ledger_db, session_id = _run_opencode(tmp_path, fixture)
    assert completed.returncode == 0
    assert fixture.calls >= 7
    assert fixture.paths and all(path.endswith("/chat/completions") for path in fixture.paths)
    # OpenCode 2.0.22 also advertises its code-mode `execute` entry. Business tools stay the governed set.
    assert fixture.tool_catalogs
    assert all(set(catalog) <= {
        "execute", "read_application", "read_documents", "extract_fields", "check_registry", "screen_sanctions",
        "compute_risk", "create_client", "request_more_docs", "escalate_edd", "reject_application",
        "send_email", "fetch_url", "run_code", "load_risk_model", "read_config",
    } and "read_application" in catalog and "delete_client" not in catalog for catalog in fixture.tool_catalogs)

    async def read_evidence():
        store = EventStore(str(evidence_db))
        await store.initialize()
        try:
            events = await store.get_events_by_session_seq(session_id)
            verification = await store.get_verification(session_id)
            pending = await store.pending_deliveries()
            return events, verification, pending
        finally:
            await store.close()

    events, verification, pending = asyncio_run(read_evidence())
    # Pending intents are stored without a consumer seq. Delivered events are contiguous from 0.
    sequenced = [event for event in events if event.seq is not None]
    assert [event.seq for event in sequenced] == list(range(len(sequenced)))
    wire = [to_consumer_v21(event) for event in sequenced]
    assert all(item["schema_version"] == "2.1" for item in wire)
    names = [item["action_details"].get("name") for item in wire if item["action_type"] == "tool_call"]
    assert names == ["read_application", "read_documents", "extract_fields", "screen_sanctions",
                     "compute_risk", "create_client"]
    with sqlite3.connect(ledger_db) as con:
        drained = con.execute("SELECT COUNT(*) FROM plugin_runs WHERE state='done'").fetchone()[0]
    assert pending == 0 and drained >= len(sequenced)
    assert verification["verification_status"] == "VERIFIED_SUCCESS"
    assert verification["checks"]
    with sqlite3.connect(bank_copy) as con:
        assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id='APP-0001'").fetchone()[0] == 1


def test_real_opencode_model_message_without_actions_finishes_incomplete(tmp_path):
    with _FixtureModel(fail_without_tools=True) as fixture:
        completed, _bank_copy, evidence_db, _ledger_db, session_id = _run_opencode(tmp_path, fixture, failure=True)
    assert completed.returncode == 2
    assert fixture.calls >= 1
    verification, events, _pending = _read_verification(evidence_db, session_id)
    assert verification["verification_status"] == "VERIFICATION_INCOMPLETE"
    assert any(event.action_type.value == "SESSION" and event.action_details.wire_details.get("phase") == "ended"
               for event in events)
