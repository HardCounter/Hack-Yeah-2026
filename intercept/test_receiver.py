"""Observe-only receiver: accepts adapter-shaped requests, flags gateway-incompatible ones."""
import asyncio
import io
import json
import unittest

from intercept.service.receiver import RECEIVER_VERSION, Receiver, check_shape

TOKEN = "x" * 32


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.out = io.StringIO()
        self.receiver = Receiver(TOKEN, compact=True, color=False, out=self.out)
        self.server = await asyncio.start_server(self.receiver.handle, "127.0.0.1", 0, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def post(self, path, body, token=TOKEN):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        data = json.dumps(body).encode()
        auth = f"Authorization: Bearer {token}\r\n" if token else ""
        writer.write(f"POST {path} HTTP/1.1\r\n{auth}Content-Length: {len(data)}\r\n\r\n".encode() + data)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        await writer.wait_closed()
        head, payload = response.split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(payload)

    async def test_tool_admission_and_outcome_are_logged_and_allowed(self):
        call = {"session_id": "ses_1", "call_id": "call_1", "tool": "read", "arguments": {"filePath": "a.md"}}
        status, reply = await self.post("/v1/actions/evaluate", call)
        self.assertEqual(status, 200)
        self.assertEqual(reply, {"decision": "ALLOW", "code": "OBSERVE_ONLY", "policy_version": RECEIVER_VERSION,
                                 "session_id": "ses_1", "call_id": "call_1", "tool": "read"})
        status, _ = await self.post("/v1/actions/outcome", {**{k: call[k] for k in ("session_id", "call_id", "tool")},
                                                             "status": "completed"})
        self.assertEqual(status, 200)
        log = self.out.getvalue()
        self.assertIn("POST /v1/actions/evaluate 200 session=ses_1 tool=read", log)
        self.assertIn('"filePath": "a.md"', log)
        self.assertIn("status=completed", log)

    async def test_prompt_and_llm_request_echo_request_id(self):
        for body in ({"action_type": "prompt", "session_id": "ses_1", "request_id": "msg_1", "message_id": "msg_1", "text": "hi"},
                     {"action_type": "llm_request", "session_id": "ses_1", "request_id": "ses_1:1", "request_seq": 1,
                      "agent": "build", "model": {"id": "m"}, "messages": []}):
            status, reply = await self.post("/v1/prompts/evaluate", body)
            self.assertEqual((status, reply["decision"], reply["request_id"]), (200, "ALLOW", body["request_id"]))
        self.assertIn("llm_request primary #1 agent=build model=m", self.out.getvalue())

    async def test_gateway_incompatible_shapes_are_rejected_and_explained(self):
        status, reply = await self.post("/v1/actions/evaluate", {"session_id": "ses_1", "call_id": "c", "tool": "read",
                                                                   "arguments": {}, "agent": "build"})
        self.assertEqual(status, 400)
        self.assertIn("shape: INVALID", self.out.getvalue())

    async def test_wrong_token_is_rejected_without_logging_the_body(self):
        status, _ = await self.post("/v1/actions/evaluate", {"secret": "DEMO_SECRET_123"}, token="y" * 32)
        self.assertEqual(status, 401)
        self.assertNotIn("DEMO_SECRET_123", self.out.getvalue())

    async def test_unknown_route_and_tool_dispatch_are_not_supported(self):
        self.assertEqual((await self.post("/v1/nope", {"a": 1}))[0], 404)
        self.assertEqual((await self.post("/v1/tools/catalog", {}))[0], 501)

    def test_check_shape(self):
        self.assertIsNone(check_shape("/v1/actions/outcome", {"session_id": "s", "call_id": "c", "tool": "t", "status": "error"}))
        self.assertIn("status", check_shape("/v1/actions/outcome", {"session_id": "s", "call_id": "c", "tool": "t", "status": "ok"}))
        self.assertIn("action_type", check_shape("/v1/prompts/evaluate", {"session_id": "s", "request_id": "r"}))

    def test_short_token_is_refused(self):
        with self.assertRaises(ValueError):
            Receiver("short")

    async def test_adapter_handshake_ready_and_failure(self):
        status, _ = await self.post("/v1/adapter/hello", {"adapter": "hardcounter.intercept", "status": "ready",
                                                           "prompts": "observe", "hooks": ["tool.execute.before"]})
        self.assertEqual(status, 200)
        self.assertIn("ADAPTER CONNECTED hardcounter.intercept prompts=observe", self.out.getvalue())
        # a setup failure reports without a token: shown, but only status and reason
        status, _ = await self.post("/v1/adapter/hello", {"status": "error", "reason": "INTERCEPT_TOKEN missing",
                                                           "extra": "DEMO_SECRET_123"}, token=None)
        self.assertEqual(status, 401)
        log = self.out.getvalue()
        self.assertIn("ADAPTER FAILED TO START status=error reason=INTERCEPT_TOKEN missing", log)
        self.assertNotIn("DEMO_SECRET_123", log)
