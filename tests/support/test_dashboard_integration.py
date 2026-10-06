"""Execute actual dashboard JavaScript against both HTTP services and real synthetic SQLite evidence."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="dashboard integration requires Node.js")
def test_dashboard_contracts_with_real_governed_evidence(tmp_path):
    # The child gets only process prerequisites and synthetic state, never provider/admin secrets.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    output = tmp_path / "server.log"
    with output.open("w+") as log:
        process = subprocess.Popen([sys.executable, str(ROOT / "tests/frontend/live_dashboard_server.py")],
                                   cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            url = None
            while time.monotonic() < deadline:
                log.seek(0)
                url = next((line.strip() for line in log if line.startswith("http://127.0.0.1:")), None)
                if process.poll() is not None:
                    raise AssertionError(f"dashboard server exited: {output.read_text()}")
                if url:
                    try:
                        with urllib.request.urlopen(url + "/healthz", timeout=1):
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                time.sleep(0.05)
            else:
                raise AssertionError(f"dashboard server did not become ready: {output.read_text()}")
            result = subprocess.run([shutil.which("node"), str(ROOT / "tests/frontend/live-dashboard-smoke.mjs"), url],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Live dashboard smoke passed" in result.stdout
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
