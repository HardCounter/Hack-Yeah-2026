import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from .auditors import Pipeline
from .config import load
from .policy import Policy
from .test_policy import action, configuration


def scanner(effect="REDACT"):
    return {"id": "scanner", "type": "pattern_scanner", "config": {"patterns": ["DEMO_SECRET_123"], "action": effect}}


class AuditorTests(unittest.IsolatedAsyncioTestCase):
    async def test_redaction_is_copy_and_evidence_has_no_payload(self):
        a = action()
        a["arguments"]["nested"] = ["prefix DEMO_SECRET_123"]
        checked, evidence, verdict, changed = await Pipeline([scanner()]).evaluate(a)
        self.assertEqual(checked["arguments"]["nested"], ["prefix [REDACTED]"])
        self.assertIn("DEMO_SECRET_123", a["arguments"]["nested"][0])
        self.assertNotIn("DEMO_SECRET_123", json.dumps(evidence))
        self.assertEqual((verdict, changed), ("ALLOW", True))

    async def test_block_cannot_be_cleared_by_later_allow(self):
        a = action()
        a["arguments"]["text"] = "DEMO_SECRET_123"
        specs = [scanner("BLOCK"), {"id": "allow", "type": "tool_allowlist", "config": {"allowed_tools": ["write"]}}]
        checked, _, verdict, _ = await Pipeline(specs).evaluate(a)
        result = Policy(configuration()).evaluate(checked, verdict)
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["tool_calls_used"], 0)

    async def test_transformed_final_arguments_are_scope_checked(self):
        c = configuration()
        c["runs"]["session-1"]["argument_equals"]["write"]["target"] = "DEMO_SECRET_123"
        checked, _, verdict, _ = await Pipeline([scanner()]).evaluate(action(target="DEMO_SECRET_123"))
        self.assertEqual(Policy(c).evaluate(checked, verdict)["code"], "TASK_SCOPE")

    async def test_alert_is_additive(self):
        a = action()
        a["arguments"]["text"] = "DEMO_SECRET_123"
        _, evidence, verdict, _ = await Pipeline([scanner("ALERT")]).evaluate(a)
        self.assertEqual(verdict, "ALLOW")
        self.assertEqual(evidence[0]["decision"], "ALERT")

    async def test_signature_match_is_case_insensitive(self):
        a = action()
        a["arguments"]["text"] = "Ignore Previous Instructions"
        specs = [{"id": "scanner", "type": "pattern_scanner", "config": {"patterns": ["ignore previous instructions"], "action": "BLOCK"}}]
        self.assertEqual((await Pipeline(specs).evaluate(a))[2], "BLOCK")

    async def test_classified_scanner_redacts_pesel_and_keeps_the_original(self):
        a = action()
        a["arguments"]["text"] = "PESEL 44051401359"
        specs = [{"id": "privacy", "type": "classified_scanner", "config": {"classes": ["pesel"], "action": "REDACT"}}]
        checked, evidence, verdict, changed = await Pipeline(specs).evaluate(a)
        self.assertEqual(checked["arguments"]["text"], "PESEL [REDACTED]")
        self.assertIn("44051401359", a["arguments"]["text"])
        self.assertEqual((verdict, changed, evidence[0]["code"]), ("ALLOW", True, "PRIVACY_MATCH"))
        self.assertNotIn("44051401359", json.dumps(evidence))

    async def test_signature_in_key_denies(self):
        a = action()
        a["arguments"]["DEMO_SECRET_123"] = "value"
        self.assertEqual((await Pipeline([scanner()]).evaluate(a))[2], "BLOCK")

    async def test_webhook_and_bad_response_fail_closed(self):
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = b'{"decision":"ALLOW","untrusted":"extra"}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        spec = {"id": "hook", "type": "webhook", "config": {"endpoint": f"http://127.0.0.1:{port}/audit", "timeout_ms": 500, "on_failure": "BLOCK"}}
        try:
            _, evidence, verdict, _ = await Pipeline([spec]).evaluate(action())
            self.assertEqual(verdict, "BLOCK")
            self.assertEqual(evidence[0]["code"], "AUDITOR_UNAVAILABLE")
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual((await Pipeline([spec]).evaluate(action()))[2], "BLOCK")

    async def test_local_webhook_allow_and_approval(self):
        verdict = "ALLOW"
        async def handler(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.lower().split(":", 1) for line in head.decode().split("\r\n")[1:] if ":" in line)
            size = int(headers["content-length"].strip())
            await reader.readexactly(size)
            body = json.dumps({"decision": verdict}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        spec = {"id": "hook", "type": "webhook", "config": {"endpoint": f"http://127.0.0.1:{port}/audit", "timeout_ms": 500, "on_failure": "BLOCK"}}
        try:
            self.assertEqual((await Pipeline([spec]).evaluate(action()))[2], "ALLOW")
            verdict = "REQUIRE_APPROVAL"
            checked, _, result, _ = await Pipeline([spec]).evaluate(action())
            self.assertEqual(result, "REQUIRE_APPROVAL")
            self.assertEqual(Policy(configuration()).evaluate(checked, result)["decision"], "BLOCK")
        finally:
            server.close()
            await server.wait_closed()


class ConfigTests(unittest.TestCase):
    def test_demo_loads_and_hash_includes_auditors(self):
        policy, pipeline = load("config/intercept.demo.yaml")
        self.assertEqual(len(pipeline.specs), 3)
        self.assertEqual(len(policy.version), 64)

    def test_duplicate_yaml_keys_and_unknown_auditors_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.yaml"
            path.write_text("runs: {}\nruns: {}\n")
            with self.assertRaises(ValueError):
                load(path)
        with self.assertRaises(ValueError):
            Pipeline([{"id": "x", "type": "arbitrary-import", "config": {}}])


if __name__ == "__main__":
    unittest.main()
