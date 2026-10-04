"""Run an isolated OpenCode KYC agent through the existing three-layer runtime."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]


def opencode_binary(override=None):
    installed = REPO / 'var/opencode-cli/node_modules/.bin/opencode'
    binary = override or (str(installed) if installed.is_file() else shutil.which('opencode'))
    if not binary:
        raise ValueError('OpenCode is missing; run scripts/setup_opencode_pipeline.sh')
    binary = shutil.which(binary) or str(Path(binary).resolve())
    version = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=15)
    if version.returncode or not re.fullmatch(r'(?:opencode v)?2\.\d+\.\d+(?:[-+][\w.-]+)?', version.stdout.strip()):
        raise ValueError('OpenCode 2.x is required; run scripts/setup_opencode_pipeline.sh or set OPENCODE_BIN')
    help_result = subprocess.run([binary, 'run', '--help'], capture_output=True, text=True, timeout=15)
    if help_result.returncode or not all(flag in help_result.stdout for flag in ('--standalone', '--session', '--auto')):
        raise ValueError('OpenCode must support run --standalone --session --auto')
    return binary


def stop_process(process):
    if process is None:
        return
    # Only signal the dedicated process group we started, including its private server.
    for sig, wait in ((signal.SIGINT, 5), (signal.SIGTERM, 3), (signal.SIGKILL, 3)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _child_env(config_home, token, admin):
    """Keep provider credentials from the process environment, but not the operator's OpenCode config."""
    env = os.environ.copy()
    for key in ('OPENCODE_CONFIG', 'OPENCODE_CONFIG_CONTENT', 'OPENCODE_CONFIG_DIR'):
        env.pop(key, None)
    config_home.mkdir(parents=True, exist_ok=True)
    env.update(
        XDG_CONFIG_HOME=str(config_home),
        INTERCEPT_TOKEN=token,
        INTERCEPT_ADMIN_TOKEN=admin,
        OPENCODE_DISABLE_DEFAULT_PLUGINS='1',
        OPENCODE_DISABLE_EXTERNAL_SKILLS='1',
        OPENCODE_DISABLE_CLAUDE_CODE='1',
        OPENCODE_DISABLE_MODELS_FETCH='1',
        OPENCODE_DISABLE_AUTOUPDATE='1',
        OPENCODE_DISABLE_LSP_DOWNLOAD='1',
    )
    return env


def _log(output, name):
    path = output / name
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    return os.fdopen(fd, 'w', encoding='utf-8')


def run_pipeline(args):
    from simulation.pipeline_support import prepare_project, discover_bound_session, request, export_run
    binary = opencode_binary(args.opencode_bin)
    parent = Path(args.output_dir).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=f'{args.application}-', dir=parent))
    output.chmod(0o700)
    bank_runs = output / 'bank-runs'
    bank_runs.mkdir()
    contract_id = f'contract_{args.application.replace("-", "")}_{secrets.token_hex(8)}'
    session_id = f'ses_{secrets.token_hex(16)}'
    token, admin = secrets.token_hex(32), secrets.token_hex(32)
    endpoint = f'http://127.0.0.1:{free_port()}'
    gateway = opencode = None
    gateway_log = opencode_log = reply_log = None
    interrupted = False
    finished = False
    result = {'verification_status': 'VERIFICATION_INCOMPLETE', 'checks': [
        {'id': 'PIPELINE', 'status': 'INCOMPLETE', 'detail': 'PIPELINE_NOT_COMPLETED'}]}
    status = 1
    work = Path(tempfile.mkdtemp(prefix='work-', dir=output))
    try:
        if args.bank_db:
            bank = Path(args.bank_db).resolve()
            if not bank.is_file():
                raise ValueError('bank database does not exist')
        else:
            from simulation import agent  # initializes existing tools/data import paths
            import generate
            dataset = work / 'dataset'
            generate.build(dataset)
            bank = dataset / 'bank.db'
        project = work / 'project'
        prepare_project(project, REPO, args.application, contract_id, args.model, endpoint,
                        provider_config=args.provider_config, free=args.free)
        child_env = _child_env(work / 'config', token, admin)
        gateway_log = _log(output, 'gateway.log')
        gateway = subprocess.Popen([
            sys.executable, '-m', 'intercept.service.local', '--bank-db', str(bank),
            '--application', args.application, '--contract-id', contract_id,
            '--runs-dir', str(bank_runs), '--port', endpoint.rsplit(':', 1)[1],
            *(['--policy', str(args.policy.resolve())] if args.policy else []),
            *(['--catalog-all'] if args.free else []),
        ], cwd=REPO, env=child_env, stdout=gateway_log, stderr=subprocess.STDOUT,
            start_new_session=True)
        deadline = time.monotonic() + 30
        while True:
            if gateway.poll() is not None:
                raise RuntimeError('Integrated gateway failed to start')
            try:
                request(endpoint, '/v1/tools/catalog', {}, token)
                break
            except (OSError, ValueError, RuntimeError):
                if time.monotonic() > deadline:
                    raise RuntimeError('Integrated gateway readiness timed out') from None
                time.sleep(0.1)
        # Trusted orchestration creates the session ID. OpenCode's --session
        # uses this exact fresh ID; no model or model-call arguments bind identity.
        request(endpoint, '/v1/runs/bind', {'session_id': session_id, 'contract_id': contract_id}, admin)
        prompt = args.prompt or (
            f'Process application {args.application}. Use the governed KYC tools and finish with exactly one decision.'
        )
        print(f'OpenCode agent: {args.agent}\nModel: {args.model}\nApplication: {args.application}\nArtifacts: {output}', flush=True)
        opencode_log = _log(output, 'opencode.log')
        # Free-agent mode keeps what the agent printed; tool results reached it only through the gateway.
        reply_log = _log(output, 'reply.txt') if args.free else None
        # OpenCode selects its project from PWD, not only the process working directory.
        opencode_env = child_env.copy()
        opencode_env['PWD'] = str(project)
        opencode = subprocess.Popen([
            binary, 'run', '--standalone', '--auto', '--session', session_id,
            '--agent', args.agent, '--model', args.model, prompt,
        ], cwd=project, env=opencode_env, stdout=reply_log or subprocess.DEVNULL, stderr=opencode_log,
            start_new_session=True)
        try:
            status = opencode.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            status = 124
            print('OpenCode timed out; stopping the agent before final verification.', flush=True)
            stop_process(opencode)
        except KeyboardInterrupt:
            status, interrupted = 130, True
            stop_process(opencode)
        stop_process(opencode)
        bound_session = discover_bound_session(bank_runs, contract_id, args.application)
        if bound_session != session_id:
            raise RuntimeError('Persisted session differs from trusted orchestration')
        result = request(endpoint, '/v1/session/finish', {'session_id': session_id}, token, timeout=30)
        finished = True
        export_run(bank_runs, session_id, output)
        if status:
            print(f'OpenCode exit status: {status}; check model access and provider configuration.', flush=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Provider errors can contain credentials: never print subprocess output or bodies.
        print(f'Pipeline failed ({type(exc).__name__}); artifacts retained at {output}', file=sys.stderr)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop_process(opencode)
        stop_process(gateway)
        for stream in (gateway_log, opencode_log, reply_log):
            if stream is not None:
                stream.close()
        (output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
        os.chmod(output / 'verification.json', 0o600)
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(result, indent=2))
    if interrupted:
        return 130
    if status == 0 and result.get('verification_status') == 'VERIFIED_SUCCESS':
        return 0
    if finished and status == 0 and result.get('verification_status') == 'VERIFICATION_INCOMPLETE':
        return 2
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog='Example: scripts/run_pipeline.sh APP-0001 --model provider/model',
    )
    parser.add_argument('application', nargs='?', default='APP-0001')
    parser.add_argument('--model', default=os.environ.get('OPENCODE_MODEL'), help='OpenCode provider/model (or OPENCODE_MODEL)')
    parser.add_argument('--agent', default='onboarding-agent', help='OpenCode agent (default: onboarding-agent)')
    parser.add_argument('--opencode-bin', default=os.environ.get('OPENCODE_BIN'))
    parser.add_argument('--output-dir', '--runs-dir', type=Path, default=REPO / 'var/pipeline-runs',
                        help='parent directory for the isolated run artifacts')
    parser.add_argument('--bank-db', type=Path, help='synthetic bank to copy; generated when omitted')
    parser.add_argument('--policy', type=Path, help='explicit legacy policy; bypass managed backend selection')
    parser.add_argument('--timeout', type=int, default=600, help='maximum agent runtime in seconds (default: 600)')
    parser.add_argument('--prompt', help='user message; default asks the agent to process the application')
    parser.add_argument('--free', action='store_true',
                        help='free-agent demo: generic prompt, every tool offered, agent output kept in reply.txt')
    parser.add_argument('--provider-config', type=Path, help='non-secret providers-only JSON; use env substitutions for keys')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'APP-\d{4}', args.application) or not 1 <= int(args.application[4:]) <= 15:
        parser.error('application must be APP-0001 through APP-0015')
    if not args.model or not re.fullmatch(r'[A-Za-z0-9_.-]+/[^\s]+', args.model):
        parser.error('select --model provider/model or set OPENCODE_MODEL')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', args.agent):
        parser.error('agent name must be a short identifier')
    if args.prompt is not None and (not args.prompt.strip() or len(args.prompt) > 4000):
        parser.error('--prompt must be a non-empty message under 4000 characters')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    try:
        return run_pipeline(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else 'Unable to launch OpenCode', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
