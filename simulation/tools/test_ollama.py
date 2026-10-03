"""Live check: a local Ollama model accepts openai_tools() and its tool call executes through registry.call().

    uv run pytest simulation/tools/test_ollama.py      (OLLAMA_URL, OLLAMA_MODEL to override)

Skipped when Ollama is not running or the model is not pulled (e.g. in CI), so it never blocks the suite.
"""
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import registry  # noqa: E402
import generate  # noqa: E402  (registry put data/ on sys.path)

URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")


def post(path, body=None, timeout=5):
    req = urllib.request.Request(URL + path, json.dumps(body).encode() if body else None, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def test_model_calls_a_tool():
    try:
        models = [m["name"].split(":")[0] for m in post("/api/tags")["models"]]
    except OSError:
        pytest.skip(f"Ollama not reachable at {URL}")
    if MODEL.split(":")[0] not in models:
        pytest.skip(f"model {MODEL} not pulled (ollama pull {MODEL})")

    allowed = registry.AGENT_TOOLS["onboarding-agent"]
    msg = post("/v1/chat/completions", {
        "model": MODEL, "temperature": 0, "seed": 7, "tools": registry.openai_tools(allowed),
        "messages": [{"role": "system", "content": "You are a KYC onboarding analyst. Always use the tools."},
                     {"role": "user", "content": "Process application APP-0001. Start by reading the application."}]},
        timeout=300)["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    assert calls, f"model answered without a tool call: {msg.get('content')!r}"

    tmp = Path(tempfile.mkdtemp())
    try:
        generate.build(tmp)
        ctx = registry.Ctx(agent="onboarding-agent", session_id="ollama-check", db=tmp / "bank.db")
        for c in calls:
            name, args = c["function"]["name"], json.loads(c["function"]["arguments"])
            assert name in allowed, name
            result = registry.call(name, args, ctx)
            assert "error" not in result, (name, args, result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-rs"]))
