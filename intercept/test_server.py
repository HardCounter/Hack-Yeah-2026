"""Loopback HTTP admission and independently read persisted audit tests."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from .policy import Policy
from .server import Evidence, Gateway
from .test_policy import action, configuration


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "evidence.jsonl"
        self.evidence = Evidence(self.path)
        self.worker = asyncio.create_task(self.evidence.worker())
        self.gateway = Gateway(Policy(configuration()), "x" * 32, self.evidence)
        self.server = await asyncio.start_server(self.gateway.handle, "127.0.0.1", 0, limit=8192)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        self.temp.cleanup()

    async def request(self, proposed, token="x" * 32, path="/v1/actions/evaluate"):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        body = json.dumps(proposed).encode()
        writer.write(f"POST {path} HTTP/1.1\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        await writer.wait_closed()
        head, data = response.split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(data)

    async def test_admission_metadata_persisted_without_payload(self):
        proposed = action()
        proposed["arguments"]["secret"] = "synthetic-secret"
        status, decision = await self.request(proposed)
        self.assertEqual(status, 200)
        self.assertEqual(decision["decision"], "ALLOW")
        await asyncio.wait_for(self.evidence.queue.join(), 5)
        saved = self.path.read_text()
        self.assertNotIn("synthetic-secret", saved)
        self.assertNotIn("arguments", saved)
        self.assertEqual(json.loads(saved)["policy_version"], self.gateway.policy.version)

    async def test_auth_invalid_action_and_audit_failure(self):
        self.assertEqual((await self.request(action(), "wrong"))[0], 401)
        self.assertEqual((await self.request({"bad": "request"}))[0], 400)
        self.evidence.failed = True
        self.assertEqual((await self.request(action()))[0], 503)
        # Failed admission is conservative: cannot reuse the reservation.
        self.evidence.failed = False
        self.assertEqual((await self.request(action()))[1]["code"], "REPLAY")

    async def test_rejected_scope_is_logged_without_side_effect(self):
        status, decision = await self.request(action(target="unrelated"))
        self.assertEqual(status, 200)
        self.assertEqual(decision["decision"], "BLOCK")
        self.assertEqual(self.gateway.policy.used.get("session-1", 0), 0)

    async def test_correlated_outcome_does_not_claim_verified_success(self):
        await self.request(action())
        observation = {"session_id": "session-1", "call_id": "call-1", "tool": "write", "status": "completed"}
        status, result = await self.request(observation, path="/v1/actions/outcome")
        self.assertEqual(status, 200)
        self.assertEqual(result["verification"], "NOT_VERIFIED")
        await asyncio.wait_for(self.evidence.queue.join(), 5)
        events = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([e["event_type"] for e in events], ["admission", "observation"])


if __name__ == "__main__":
    unittest.main()
