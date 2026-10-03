"""Small wrapper checks for the local OpenCode pipeline launchers."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_all_scripts_have_valid_bash_syntax_and_are_executable():
    scripts = sorted((ROOT / "scripts").glob("*.sh"))
    assert scripts
    for script in scripts:
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script.name}: {result.stderr}"
        assert os.access(script, os.X_OK), f"{script.name} is not executable"


def test_setup_help_does_not_require_uv_or_npm(tmp_path):
    env = {**os.environ, "PATH": str(tmp_path)}
    bash = shutil.which("bash")
    assert bash
    result = subprocess.run([bash, str(ROOT / "scripts/setup_opencode_pipeline.sh"), "--help"],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert "project-local OpenCode CLI" in result.stdout


def test_runner_help_does_not_require_an_opencode_binary():
    result = subprocess.run([sys_executable(), "-m", "simulation.opencode_runner", "--help"],
                            cwd=ROOT, env={**os.environ, "OPENCODE_BIN": "/missing/opencode"},
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_run_wrapper_clears_virtualenv_and_forwards_arguments(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv-call.txt"
    stub = bin_dir / "uv"
    stub.write_text(
        '#!/bin/bash\n'
        'printf "cwd=%s\\nvirtualenv=%s\\n" "$PWD" "${VIRTUAL_ENV-<unset>}" > "$PIPELINE_TEST_LOG"\n'
        'printf "arg=<%s>\\n" "$@" >> "$PIPELINE_TEST_LOG"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "VIRTUAL_ENV": "stale-env",
           "PIPELINE_TEST_LOG": str(log)}
    result = subprocess.run(["bash", str(ROOT / "scripts/run_pipeline.sh"), "--help"],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert recorded[:2] == [f"cwd={ROOT}", "virtualenv=<unset>"]
    assert recorded[2:] == [
        "arg=<run>", "arg=<--locked>", "arg=<python>", "arg=<-m>",
        "arg=<simulation.opencode_runner>", "arg=<--help>",
    ]


def sys_executable() -> str:
    import sys
    return sys.executable


def test_live_pipeline_help_and_busy_port_check():
    result = subprocess.run([sys_executable(), "-m", "simulation.live_pipeline", "--help"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0 and "--model" in result.stdout
    import socket
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        result = subprocess.run([sys_executable(), "-m", "simulation.live_pipeline", "--model", "p/m",
                                 "--port", str(port)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2 and "in use" in result.stderr
