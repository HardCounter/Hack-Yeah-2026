"""Real synthetic SQLite side effects through HTTP, independently read afterward."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from intercept.tools.execution import ToolExecutor
from intercept.policy.runs import Policy
from intercept.service.server import Evidence, Gateway
import test_server

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))
import generate
from postconditions import calls_from_audit, verify_onboarding


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    request = test_server.ServerTests.request

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        await asyncio.to_thread(generate.build, self.root)
        self.db = self.root / "bank.db"
        self.evidence = Evidence(self.root / "events.jsonl")
        self.worker = asyncio.create_task(self.evidence.worker())
        tools = ["request_more_docs", "create_client"]
        policy = Policy({"runs": {"session-1": {"contract_id": "synthetic-1", "allowed_tools": tools,
            "tool_call_budget": 10, "require_approval": [],
            "argument_equals": {t: {"app_id": "APP-0001"} for t in tools}}}})
        self.gateway = Gateway(policy, "x" * 32, self.evidence, executor=ToolExecutor(self.db))
        self.server = await asyncio.start_server(self.gateway.handle, "127.0.0.1", 0, limit=8192)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        self.temp.cleanup()

    def proposal(self, call, app="APP-0001", tool="request_more_docs", extra=None):
        args = {"app_id": app, "reason": "synthetic fixture"} if tool == "request_more_docs" else {"app_id": app, "fields": extra}
        return {"session_id": "session-1", "call_id": call, "tool": tool, "arguments": args}

    def query(self, sql, *params):
        with closing(sqlite3.connect(self.db)) as con:
            return con.execute(sql, params).fetchall()

    async def test_allowed_persisted_once_concurrent_replay_denied(self):
        proposal = self.proposal("once")
        responses = await asyncio.gather(*(self.request(proposal, path="/v1/tools/execute") for _ in range(2)))
        self.assertEqual(sum(r[1].get("decision") == "ALLOW" for r in responses), 1)
        self.assertEqual(self.query("SELECT status FROM onboarding_applications WHERE application_id='APP-0001'"), [("more_docs_requested",)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM audit_actions WHERE session_id='session-1'"), [(1,)])
        self.assertEqual(self.query("SELECT agent FROM audit_actions WHERE session_id='session-1'"), [("onboarding-agent",)])

    async def test_wrong_scope_never_executes_and_audit_failure_prevents_write(self):
        before = self.query("SELECT status FROM onboarding_applications WHERE application_id='APP-0002'")
        _, denied = await self.request(self.proposal("wrong", "APP-0002"), path="/v1/tools/execute")
        self.assertEqual(denied["code"], "TASK_SCOPE")
        self.assertEqual(self.query("SELECT status FROM onboarding_applications WHERE application_id='APP-0002'"), before)
        self.assertEqual(self.query("SELECT COUNT(*) FROM audit_actions"), [(0,)])
        self.evidence.failed = True
        self.assertEqual((await self.request(self.proposal("disk-fail"), path="/v1/tools/execute"))[0], 503)
        self.assertEqual(self.query("SELECT COUNT(*) FROM audit_actions"), [(0,)])

    async def test_tool_success_is_not_business_verification(self):
        with closing(sqlite3.connect(self.db)) as con:
            declared = json.loads(con.execute("SELECT declared FROM onboarding_applications WHERE application_id='APP-0001'").fetchone()[0])
        _, result = await self.request(self.proposal("create", tool="create_client",
            extra={"name": declared["name"], "dob": declared["date_of_birth"], "nationality": declared["nationality"]}), path="/v1/tools/execute")
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["verification"], "NOT_VERIFIED")
        self.assertIn("client_id", result["tool_result"])
        # Deliberately incomplete workflow: no screening. Independent verifier detects it.
        with closing(sqlite3.connect(self.db)) as con:
            checks = verify_onboarding(con, "APP-0001", calls_from_audit(con, "session-1"))
        self.assertTrue(any(c["id"] == "ONB-P1" and c["ok"] is False for c in checks))

    async def test_execution_requires_operator_opt_in(self):
        self.gateway.executor = None
        self.assertEqual((await self.request(self.proposal("off"), path="/v1/tools/execute"))[0], 503)
        self.assertEqual(self.query("SELECT COUNT(*) FROM audit_actions"), [(0,)])

    async def test_admin_binding_and_real_registry_catalog(self):
        self.gateway.admin_token = "a" * 32
        binding = {"session_id": "ses_bound", "contract_id": "synthetic-1"}
        self.assertEqual((await self.request(binding, path="/v1/runs/bind"))[0], 401)
        status, bound = await self.request(binding, token="a" * 32, path="/v1/runs/bind")
        self.assertEqual(status, 200)
        self.assertEqual(bound["session_id"], "ses_bound")
        status, catalog = await self.request({}, path="/v1/tools/catalog")
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in catalog["tools"]}
        self.assertEqual(names, {"request_more_docs", "create_client"})
        tool = next(t for t in catalog["tools"] if t["name"] == "request_more_docs")
        self.assertEqual(tool["input"]["required"], ["app_id", "reason"])
        request = {**self.proposal("bound-write"), "session_id": "ses_bound"}
        self.assertEqual((await self.request(request, path="/v1/tools/execute"))[1]["decision"], "ALLOW")
        self.assertEqual(self.query("SELECT session_id FROM audit_actions"), [("ses_bound",)])


if __name__ == "__main__":
    unittest.main()
