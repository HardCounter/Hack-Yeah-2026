"""Bounded loopback HTTP admission service using Python asyncio only."""
import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path
from .policy import Policy
from .auditors import Pipeline
from .config import load
from .execution import ToolExecutor


class Evidence:
    """Bounded, non-durable ingestion queue; disk writes occur off the event loop."""
    def __init__(self, path):
        self.path = Path(path)
        self.queue = asyncio.Queue(maxsize=1024)
        self.failed = False

    def append(self, event):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")

    async def worker(self):
        while True:
            event = await self.queue.get()
            try:
                await asyncio.to_thread(self.append, event)
            except OSError:
                self.failed = True
            finally:
                self.queue.task_done()

    def enqueue(self, event):
        if self.failed:
            raise RuntimeError("audit unavailable")
        self.queue.put_nowait(event)


class Gateway:
    def __init__(self, policy, token, evidence, pipeline=None, trace=False, executor=None):
        if not isinstance(token, str) or len(token) < 32 or not token.isascii():
            raise ValueError("a local ASCII bearer token of at least 32 characters is required")
        self.policy, self.token, self.evidence = policy, token, evidence
        self.pipeline = pipeline or Pipeline([])
        self.trace = trace
        self.executor = executor

    async def handle(self, reader, writer):
        status, result = 400, {"code": "INVALID_REQUEST"}
        try:
            async with asyncio.timeout(3):
                head = await reader.readuntil(b"\r\n\r\n")
                if len(head) > 8192:
                    raise ValueError("headers too large")
                lines = head.decode("ascii").split("\r\n")
                method, path, protocol = lines[0].split(" ")
                headers = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    key, value = line.split(":", 1)
                    key = key.lower()
                    if key in headers:
                        raise ValueError("duplicate header")
                    headers[key] = value.strip()
                if "transfer-encoding" in headers or protocol != "HTTP/1.1":
                    raise ValueError("unsupported framing")
                if not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self.token):
                    status, result = 401, {"code": "UNAUTHORIZED"}
                elif method != "POST" or path not in ("/v1/actions/evaluate", "/v1/actions/outcome", "/v1/tools/execute"):
                    status, result = 404, {"code": "NOT_FOUND"}
                elif path == "/v1/tools/execute" and self.executor is None:
                    status, result = 503, {"code": "TOOL_EXECUTION_DISABLED"}
                else:
                    size = int(headers.get("content-length", "0"))
                    if not 0 < size <= 65536:
                        status, result = 413, {"code": "BODY_LIMIT"}
                    else:
                        action = json.loads(await reader.readexactly(size))
                        event_type = "observation" if path.endswith("outcome") else "admission"
                        modified = None
                        if event_type == "admission":
                            self.policy.validate_action(action)
                            # Initial hard checks cannot be relaxed by later transformations.
                            initial = self.policy.evaluate(action, reserve=False)
                            if initial["decision"] == "BLOCK":
                                checked, decisions, verdict, changed = action, [], "ALLOW", False
                            else:
                                checked, decisions, verdict, changed = await self.pipeline.evaluate(action)
                            result = self.policy.evaluate(checked, verdict)
                            result["auditor_decisions"] = decisions
                            if changed and result["decision"] == "ALLOW":
                                modified = checked["arguments"]
                        else:
                            result = self.policy.observe(action)
                        # No raw arguments, results, credentials, or free-form reasons.
                        self.evidence.enqueue({"event_type": event_type, **result})
                        if self.trace:
                            print(json.dumps({"event_type": event_type, **result}), flush=True)
                        if path == "/v1/tools/execute" and result["decision"] == "ALLOW":
                            tool_result = await self.executor.execute(checked)
                            observed = self.policy.observe({"session_id": checked["session_id"],
                                "call_id": checked["call_id"], "tool": checked["tool"],
                                "status": "error" if "error" in tool_result else "completed"})
                            self.evidence.enqueue({"event_type": "observation", **observed})
                            if self.trace:
                                print(json.dumps({"event_type": "observation", **observed}), flush=True)
                            result = {**result, "tool_result": tool_result, "verification": "NOT_VERIFIED"}
                        if modified is not None:
                            # Return transformations to the adapter, NEVER persist raw arguments.
                            result = {**result, "modified_arguments": modified}
                        status = 200
        except (ValueError, TypeError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, RecursionError):
            status, result = 400, {"code": "INVALID_REQUEST"}
        except (asyncio.QueueFull, RuntimeError):
            status, result = 503, {"code": "AUDIT_UNAVAILABLE"}
        except TimeoutError:
            status, result = 408, {"code": "TIMEOUT"}
        finally:
            try:
                data = json.dumps(result).encode()
                writer.write(f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            await writer.wait_closed()


async def serve(policy_path, audit_path, port, trace=False, database=None, tool_agent="onboarding-agent"):
    policy, pipeline = load(policy_path)
    evidence = Evidence(audit_path)
    executor = ToolExecutor(database, tool_agent) if database else None
    gateway = Gateway(policy, os.environ.get("INTERCEPT_TOKEN", ""), evidence, pipeline, trace, executor)
    worker = asyncio.create_task(evidence.worker())
    server = await asyncio.start_server(gateway.handle, "127.0.0.1", port, limit=8192)
    try:
        async with server:
            await server.serve_forever()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--trace", action="store_true", help="print sanitized pipeline evidence to the terminal")
    parser.add_argument("--bank-db", help="opt in to synthetic Python tool execution against this database")
    parser.add_argument("--tool-agent", default="onboarding-agent", choices=["onboarding-agent", "admin-agent"])
    args = parser.parse_args()
    asyncio.run(serve(args.policy, args.audit, args.port, args.trace, args.bank_db, args.tool_agent))


if __name__ == "__main__":
    main()
