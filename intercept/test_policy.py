"""Deterministic tests; no provider/model, credentials, or customer systems."""
import concurrent.futures
import unittest
from .policy import Policy


def configuration(budget=2):
    return {"runs": {"session-1": {
        "contract_id": "contract-1", "allowed_tools": ["write", "approve"],
        "tool_call_budget": budget, "require_approval": ["approve"],
        "argument_equals": {"write": {"target": "assigned"}},
    }}}


def action(call="call-1", tool="write", target="assigned"):
    return {"session_id": "session-1", "call_id": call, "tool": tool,
            "arguments": {"target": target}}


class PolicyTests(unittest.TestCase):
    def test_allow_replay_and_budget(self):
        policy = Policy(configuration(1))
        self.assertEqual(policy.evaluate(action())["decision"], "ALLOW")
        self.assertEqual(policy.evaluate(action())["code"], "REPLAY")
        self.assertEqual(policy.evaluate(action("call-2"))["code"], "BUDGET_EXHAUSTED")

    def test_scope_permissions_approval_and_unknown_run(self):
        policy = Policy(configuration())
        self.assertEqual(policy.evaluate(action(target="unrelated"))["code"], "TASK_SCOPE")
        self.assertEqual(policy.evaluate(action("c2", "shell"))["code"], "TOOL_DENIED")
        self.assertEqual(policy.evaluate(action("c3", "approve"))["code"], "APPROVAL_NOT_IMPLEMENTED")
        unknown = {**action("c4"), "session_id": "unregistered"}
        self.assertEqual(policy.evaluate(unknown)["code"], "UNKNOWN_RUN")

    def test_concurrent_admissions_are_atomic(self):
        policy = Policy(configuration(3))
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda i: policy.evaluate(action(f"c{i}")), range(20)))
        self.assertEqual(sum(r["decision"] == "ALLOW" for r in results), 3)

    def test_trusted_snapshot_and_sanitized_decision(self):
        config = configuration()
        policy = Policy(config)
        config["runs"]["session-1"]["allowed_tools"].append("shell")
        proposed = action()
        proposed["arguments"]["secret"] = "synthetic-secret-must-not-be-logged"
        result = policy.evaluate(proposed)
        self.assertNotIn("synthetic-secret", str(result))
        self.assertEqual(policy.evaluate(action("c2", "shell"))["code"], "TOOL_DENIED")
        self.assertEqual(len(result["policy_version"]), 64)

    def test_invalid_data_and_boolean_integer_constraint(self):
        policy = Policy(configuration())
        with self.assertRaises(ValueError):
            policy.evaluate({**action(), "policy_version": "attacker"})
        with self.assertRaises(ValueError):
            Policy(configuration(-1))
        config = configuration()
        config["runs"]["session-1"]["argument_equals"]["write"] = {"target": 1}
        self.assertEqual(Policy(config).evaluate(action(target=True))["code"], "TASK_SCOPE")

    def test_observation_matches_admission_and_is_not_verification(self):
        policy = Policy(configuration())
        observed = {"session_id": "session-1", "call_id": "call-1", "tool": "write", "status": "completed"}
        with self.assertRaises(ValueError):
            policy.observe(observed)
        policy.evaluate(action())
        self.assertEqual(policy.observe(observed)["verification"], "NOT_VERIFIED")
        with self.assertRaises(ValueError):
            policy.observe(observed)


if __name__ == "__main__":
    unittest.main()
