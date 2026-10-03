"""Create operator-selected configs for actual OpenCode sessions; no invented session/model IDs."""
import argparse
import json
from pathlib import Path
import yaml
from .config import load


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True, help="new demo workspace outside this repository")
    parser.add_argument("--policy-directory", type=Path, required=True, help="operator-owned directory outside the agent workspace")
    parser.add_argument("--contract", required=True)
    parser.add_argument("--application", required=True)
    parser.add_argument("--model", required=True, help="an installed/configured provider/model identifier")
    parser.add_argument("--budget", type=int, default=20)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = args.directory.resolve()
    policy_dir = args.policy_directory.resolve()
    if destination == root or root in destination.parents or destination.exists():
        parser.error("use a new workspace outside the repository")
    if policy_dir == destination or destination in policy_dir.parents or policy_dir == root or root in policy_dir.parents:
        parser.error("trusted policy must be outside the demo workspace and repository")
    tools = ["read_application", "request_more_docs"]
    config = {"schema_version": 1, "runs": {}, "contracts": {
        args.contract: {"contract_id": args.contract, "allowed_tools": tools,
            "tool_call_budget": args.budget, "require_approval": [],
            "argument_equals": {t: {"app_id": args.application} for t in tools}}},
        "auditors": [{"id": "tool-scope", "type": "tool_allowlist", "config": {"allowed_tools": tools}}]}
    destination.mkdir(parents=True, mode=0o700)
    policy_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    policy_path = policy_dir / "intercept.yaml"
    try:
        policy_path.write_text(yaml.safe_dump(config, sort_keys=False))
        load(policy_path)
        opencode = {"$schema": "https://opencode.ai/config.json", "model": args.model,
            "plugins": [{"package": str(root / "adapters" / "opencode"),
                         "options": {"endpoint": "http://127.0.0.1:8080", "registerTools": True,
                                     "contractId": args.contract}}],
            "agents": {"build": {"permissions": [
                {"action": "shell", "resource": "*", "effect": "deny"},
                {"action": "edit", "resource": "*", "effect": "deny"}
            ]}}}
        (destination / "opencode.json").write_text(json.dumps(opencode, indent=2) + "\n")
        policy_path.chmod(0o600)
    except Exception:
        policy_path.unlink(missing_ok=True)
        raise
    print(f"Created OpenCode workspace: {destination}")
    print(f"Stored operator policy outside workspace: {policy_path}")
    print("Bind an empty OpenCode session by invoking /intercept-run in that session.")


if __name__ == "__main__":
    main()
