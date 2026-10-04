"""Small wrapper checks for the local OpenCode pipeline launchers."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
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


def test_intercepted_launcher_uses_selected_workspace_and_keeps_run_config(tmp_path):
    fake_root = tmp_path / "repo"
    scripts = fake_root / "scripts"
    demo = fake_root / "var" / "demo"
    workspace = tmp_path / "team workspace"
    bin_dir = tmp_path / "bin"
    scripts.mkdir(parents=True)
    demo.mkdir(parents=True)
    (demo / ".opencode").mkdir()
    workspace.mkdir()
    bin_dir.mkdir()
    launcher = scripts / "run_opencode_intercepted.sh"
    launcher.write_text((ROOT / "scripts/run_opencode_intercepted.sh").read_text(encoding="utf-8"),
                        encoding="utf-8")
    (demo / "opencode.json").write_text(
        '{"plugins":[{"package": "/tmp/adapters/opencode"}]}\n', encoding="utf-8")
    capture = tmp_path / "opencode-call.txt"
    stub = bin_dir / "opencode"
    stub.write_text(
        '#!/bin/bash\n'
        'printf "cwd=%s\\nconfig=%s\\nconfig_dir=%s\\ndisable_project=%s\\n" '
        '"$PWD" "$OPENCODE_CONFIG" "$OPENCODE_CONFIG_DIR" "$OPENCODE_DISABLE_PROJECT_CONFIG" '
        '> "$LAUNCH_CAPTURE"\n'
        'printf "arg=<%s>\\n" "$@" >> "$LAUNCH_CAPTURE"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    with socket.socket() as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.listen()
        port = receiver.getsockname()[1]
        (fake_root / "var" / "intercept.env").write_text(
            f"INTERCEPT_TOKEN={'a' * 48}\nINTERCEPT_PORT={port}\nINTERCEPT_DEMO_DIR={demo}\n",
            encoding="utf-8",
        )
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
               "LAUNCH_CAPTURE": str(capture)}
        result = subprocess.run(
            ["bash", str(launcher), "--print-logs", "--workspace", str(workspace)],
            cwd=tmp_path, env=env, capture_output=True, text=True,
        )

    assert result.returncode == 0, result.stderr
    recorded = capture.read_text(encoding="utf-8").splitlines()
    assert recorded[:4] == [
        f"cwd={workspace}", f"config={demo / 'opencode.json'}",
        f"config_dir={demo / '.opencode'}", "disable_project=1",
    ]
    assert recorded[4:] == ["arg=<--standalone>", "arg=<--print-logs>"]
    assert workspace.is_dir()


def test_intercepted_launcher_rejects_missing_workspace(tmp_path):
    fake_root = tmp_path / "repo"
    scripts = fake_root / "scripts"
    demo = fake_root / "var" / "demo"
    scripts.mkdir(parents=True)
    demo.mkdir(parents=True)
    launcher = scripts / "run_opencode_intercepted.sh"
    launcher.write_text((ROOT / "scripts/run_opencode_intercepted.sh").read_text(encoding="utf-8"),
                        encoding="utf-8")
    (fake_root / "var" / "intercept.env").write_text(
        f"INTERCEPT_TOKEN={'a' * 48}\nINTERCEPT_PORT=1\nINTERCEPT_DEMO_DIR={demo}\n",
        encoding="utf-8",
    )
    result = subprocess.run(["bash", str(launcher), "--workspace"], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "--workspace requires a directory" in result.stderr
