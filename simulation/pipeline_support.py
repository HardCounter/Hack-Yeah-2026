"""Safe project setup and sanitized evidence export for the standalone OpenCode runner."""
from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from persistence.adapters.consumer_v21 import to_consumer_v21
from persistence.models import ActionEventEnvelope
from persistence.privacy import sanitize_event

ROOT = Path(__file__).resolve().parents[1]
_IDENT = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_APP = re.compile(r"^APP-[0-9]{4}$")
_ALLOWED_PATHS = {"/v1/session/finish", "/v1/runs/bind", "/v1/tools/catalog"}


def _loopback_endpoint(endpoint: str) -> str:
    from urllib.parse import urlsplit
    if not isinstance(endpoint, str):
        raise ValueError("endpoint must be a loopback HTTP URL")
    parsed = urlsplit(endpoint)
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError):
        loopback = False
    if (parsed.scheme != "http" or not loopback
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or not parsed.port):
        raise ValueError("endpoint must be a loopback HTTP URL")
    return f"http://{parsed.netloc}"


def request(endpoint, path, payload, token, timeout=5) -> dict:
    """POST JSON to one fixed local service route without reflecting server details."""
    base = _loopback_endpoint(endpoint)
    if path not in _ALLOWED_PATHS or not isinstance(payload, dict):
        raise ValueError("unsupported local runtime request")
    if not isinstance(token, str) or len(token) < 32 or timeout <= 0 or timeout > 30:
        raise ValueError("invalid local runtime request credentials or timeout")
    req = Request(base + path, data=json.dumps(payload).encode("utf-8"), method="POST",
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read(1_000_001))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError("local runtime request failed") from None
    if not isinstance(result, dict):
        raise RuntimeError("local runtime returned an invalid response")
    return result


def _validate_provider_config(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("provider config must be readable JSON") from None
    if not isinstance(data, dict) or set(data) != {"providers"} or not isinstance(data["providers"], dict):
        raise ValueError("provider config must contain only a providers object")
    def inspect(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                low = str(k).lower().replace("_", "")
                if low in {"authorization", "token", "accesstoken", "password", "secret", "apikey"}:
                    if low == "apikey" and isinstance(v, str) and re.fullmatch(r"\{env:[A-Z0-9_]+\}", v):
                        continue
                    raise ValueError("provider config must not contain inline credentials")
                inspect(v, str(k))
        elif isinstance(value, list):
            for v in value: inspect(v, key)
        elif isinstance(value, str) and re.search(r"(?i)bearer\s+[A-Za-z0-9._~-]{16,}", value):
            raise ValueError("provider config must not contain inline credentials")
    inspect(data)
    return data


def prepare_project(project, repo, appid, contractid, model, endpoint, provider_config=None):
    """Create an isolated, gateway-only OpenCode project; never copies repository context."""
    project, repo = Path(project).resolve(), Path(repo).resolve()
    if project == repo or repo in project.parents or project in repo.parents:
        raise ValueError("OpenCode project must be isolated from the repository")
    if not _APP.fullmatch(str(appid)) or not _IDENT.fullmatch(str(contractid)):
        raise ValueError("invalid application or contract identifier")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("a model identifier is required")
    endpoint = _loopback_endpoint(endpoint)
    providers = _validate_provider_config(provider_config)
    # Import through the existing simulation module so tool signatures stay aligned with policy.
    from simulation.agent import SYSTEM, _signatures
    policy = json.loads((ROOT / "simulation" / "policy.json").read_text(encoding="utf-8"))
    prompt = SYSTEM.format(signatures=_signatures(policy["allowed_tools"]))
    config = {
        "$schema": "https://opencode.ai/config.json",
        "default_agent": "onboarding-agent",
        "model": model,
        "share": "disabled",
        "plugins": [{"package": (ROOT / "adapters" / "opencode").as_posix(), "options": {
            "endpoint": endpoint, "prompts": "off", "registerTools": True,
            "contractId": contractid,
        }}],
        "agents": {"onboarding-agent": {"mode": "primary", "prompt": prompt, "tools": {}}},
        **providers,
    }
    project.mkdir(parents=True, exist_ok=True)
    target = project / "opencode.json"
    _atomic_write(target, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    # OpenCode 2.0.22 loads the agent body from this file; the JSON prompt field is not retained.
    agent_dir = project / ".opencode" / "agents"
    agent_dir.mkdir(parents=True)
    allowed = "\n".join(f"  {name}: allow" for name in policy["allowed_tools"])
    _atomic_write(agent_dir / "onboarding-agent.md",
                  "---\n"
                  "description: Governed KYC onboarding analyst. Tools execute only through the local control gateway.\n"
                  "mode: primary\n"
                  "permission:\n"
                  "  \"*\": deny\n"
                  f"{allowed}\n"
                  "---\n\n"
                  f"{prompt}\n")
    return target


def _atomic_write(path: Path, text: str):
    fd, temp_name = tempfile.mkstemp(prefix=".pipeline-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        try: os.unlink(temp_name)
        except FileNotFoundError: pass


def discover_bound_session(runs, contract_id, appid) -> str | None:
    """Return the single run whose protected baseline is pinned to this contract and app."""
    runs = Path(runs)
    if not _IDENT.fullmatch(str(contract_id)) or not _APP.fullmatch(str(appid)):
        raise ValueError("invalid contract or application identifier")
    matches = []
    for path in sorted(runs.glob("*.bank.db")):
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                row = con.execute("SELECT session_id,contract_id,baseline_json FROM governed_baselines").fetchall()
            for session_id, stored_contract, raw in row:
                baseline = json.loads(raw)
                if (stored_contract == contract_id and baseline.get("application_id") == appid
                        and isinstance(session_id, str) and path.name == session_id + ".bank.db"):
                    matches.append(session_id)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            continue
    if len(matches) > 1:
        raise RuntimeError("multiple bound runs match the requested contract and application")
    return matches[0] if matches else None


def _read_only(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def export_run(runs, session, output):
    """Export only canonical events and sanitized findings, contract, and verifier metadata."""
    if not isinstance(session, str) or not _IDENT.fullmatch(session):
        raise ValueError("invalid session identifier")
    runs, output = Path(runs), Path(output).resolve()
    evidence = runs / f"{session}.evidence.db"
    bank = runs / f"{session}.bank.db"
    if not evidence.is_file():
        raise FileNotFoundError("run evidence database is missing")
    with _read_only(evidence) as con:
        event_rows = con.execute("SELECT payload_json FROM events WHERE session_id=? ORDER BY seq", (session,)).fetchall()
        contract_row = con.execute("SELECT contract_json FROM task_contracts WHERE session_id=?", (session,)).fetchone()
        finding_rows = con.execute("SELECT finding_id,payload_json FROM consumer_findings WHERE session_id=? ORDER BY rowid", (session,)).fetchall()
        verification_row = con.execute("SELECT payload_json FROM verification_results WHERE session_id=?", (session,)).fetchone()
    events = []
    for (raw,) in event_rows:
        event = sanitize_event(ActionEventEnvelope.from_json(raw))
        if event.status.value != "PENDING":
            events.append(to_consumer_v21(event))
    # Store-side projections are already fixed-code, privacy-limited structures. Re-project defensively.
    findings = []
    for finding_id, raw in finding_rows:
        item = json.loads(raw)
        findings.append({"finding_id": finding_id, **item})
    contract = json.loads(contract_row[0]) if contract_row else None
    verification = json.loads(verification_row[0]) if verification_row else None
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    files = {
        "events.jsonl": "".join(json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n" for item in events),
        "findings.jsonl": "".join(json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n" for item in findings),
        "contract.json": json.dumps(contract, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        "verification.json": json.dumps(verification, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
    }
    for name, content in files.items():
        _atomic_write(output / name, content)
    return output
