"""Bounded loopback HTTP admission service using Python asyncio only."""
import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path
from .policy import Policy


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
    def __init__(self, policy, token, evidence):
        if not isinstance(token, str) or len(token) < 32 or not token.isascii():
            raise ValueError("a local ASCII bearer token of at least 32 characters is required")
        self.policy, self.token, self.evidence = policy, token, evidence

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
                elif method != "POST" or path not in ("/v1/actions/evaluate", "/v1/actions/outcome"):
                    status, result = 404, {"code": "NOT_FOUND"}
                else:
                    size = int(headers.get("content-length", "0"))
                    if not 0 < size <= 65536:
                        status, result = 413, {"code": "BODY_LIMIT"}
                    else:
                        action = json.loads(await reader.readexactly(size))
                        event_type = "admission" if path.endswith("evaluate") else "observation"
                        result = self.policy.evaluate(action) if event_type == "admission" else self.policy.observe(action)
                        # No raw arguments, results, credentials, or free-form reasons.
                        self.evidence.enqueue({"event_type": event_type, **result})
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


async def serve(policy_path, audit_path, port):
    policy = Policy(json.loads(Path(policy_path).read_text()))
    evidence = Evidence(audit_path)
    gateway = Gateway(policy, os.environ.get("INTERCEPT_TOKEN", ""), evidence)
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
    args = parser.parse_args()
    asyncio.run(serve(args.policy, args.audit, args.port))


if __name__ == "__main__":
    main()
