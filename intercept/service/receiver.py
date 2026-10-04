"""Observe-only receiver: logs every OpenCode adapter request and allows it. Enforces nothing.

Use it to validate, by hand, that a live OpenCode session reaches the Python side and that the
adapter's requests match the shapes the enforcing Gateway (server.py) accepts. Each request is
checked against those shapes and the result is printed, but the reply is always ALLOW.

    INTERCEPT_TOKEN=... uv run python -m intercept.service.receiver --port 8080
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime

from intercept.policy.runs import IDENTIFIER, Policy

# Stable marker so the adapter's 64-hex policy_version check passes; it identifies this mode, not a policy.
RECEIVER_VERSION = hashlib.sha256(b"intercept.service.receiver observe-only").hexdigest()
GATEWAY_BODY_LIMIT = 65536  # what server.py accepts; larger bodies are logged with a warning
BODY_LIMIT = 4 * 1024 * 1024
PROMPT_TYPES = ("prompt", "llm_request")
HELLO = "/v1/adapter/hello"
HELLO_LOGGED = object()  # sentinel: the handshake printed its own line
ROUTES = ("/v1/actions/evaluate", "/v1/actions/outcome", "/v1/prompts/evaluate",
          "/v1/runs/bind", "/v1/tools/catalog", "/v1/tools/execute")

BOLD, DIM, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def check_shape(path, body):
    """Return None if `body` matches what the enforcing Gateway accepts on `path`, else the reason."""
    try:
        if path == "/v1/actions/evaluate":
            Policy.validate_action(body)
        elif path == "/v1/actions/outcome":
            if not isinstance(body, dict) or set(body) != {"session_id", "call_id", "tool", "status"}:
                return "outcome must have exactly session_id, call_id, tool, status"
            if any(not isinstance(body[k], str) or not IDENTIFIER.fullmatch(body[k]) for k in ("session_id", "call_id", "tool")):
                return "invalid outcome identifier"
            if body["status"] not in ("completed", "error"):
                return "status must be completed or error"
        elif path == "/v1/prompts/evaluate":
            if not isinstance(body, dict) or body.get("action_type") not in PROMPT_TYPES:
                return f"action_type must be one of {PROMPT_TYPES}"
            for k in ("session_id", "request_id"):
                if not isinstance(body.get(k), str) or not IDENTIFIER.fullmatch(body[k]):
                    return f"invalid {k}"
            # server.py has no prompt endpoint yet; this shape is the adapter's (docs/intercept/opencode-forwarding.md)
        return None
    except ValueError as e:
        return str(e)


def reply_for(path, body):
    if path == "/v1/actions/evaluate":
        return {"decision": "ALLOW", "code": "OBSERVE_ONLY", "policy_version": RECEIVER_VERSION,
                "session_id": body["session_id"], "call_id": body["call_id"], "tool": body["tool"]}
    if path == "/v1/actions/outcome":
        return {**body, "policy_version": RECEIVER_VERSION, "verification": "NOT_VERIFIED"}
    return {"decision": "ALLOW", "code": "OBSERVE_ONLY", "policy_version": RECEIVER_VERSION,
            "session_id": body["session_id"], "request_id": body["request_id"]}


def summary(path, body):
    if not isinstance(body, dict):
        return ""
    if path == "/v1/actions/evaluate":
        return f"tool={body.get('tool')} call={body.get('call_id')}"
    if path == "/v1/actions/outcome":
        return f"tool={body.get('tool')} call={body.get('call_id')} status={body.get('status')}"
    if body.get("action_type") == "prompt":
        return f"user prompt msg={body.get('message_id')} chars={len(body.get('text', ''))}"
    if body.get("action_type") == "llm_request":
        return (f"llm_request {body.get('kind', 'primary')} #{body.get('request_seq')} agent={body.get('agent')} "
                f"model={(body.get('model') or {}).get('id')} new_messages={len(body.get('messages', []))}")
    return ""


class Receiver:
    def __init__(self, token, *, compact=False, color=True, out=sys.stdout):
        if not isinstance(token, str) or len(token) < 32 or not token.isascii():
            raise ValueError("INTERCEPT_TOKEN must be at least 32 ASCII characters")
        self.token, self.compact, self.out = token, compact, out
        self.c = (lambda code: code) if color else (lambda code: "")
        self.counts = {}

    def log(self, status, path, body, problem):
        c = self.c
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        session = body.get("session_id", "-") if isinstance(body, dict) else "-"
        colour = GREEN if status == 200 else RED
        print(f"\n{c(DIM)}{stamp}{c(RESET)} {c(BOLD)}POST {path}{c(RESET)} {c(colour)}{status}{c(RESET)} "
              f"session={session} {summary(path, body)}", file=self.out)
        if problem:
            print(f"  {c(YELLOW)}shape: INVALID for the enforcing gateway: {problem}{c(RESET)}", file=self.out)
        if body is not None:
            text = json.dumps(body, ensure_ascii=False) if self.compact else json.dumps(body, indent=2, ensure_ascii=False)
            print("  " + text.replace("\n", "\n  "), file=self.out)
        self.out.flush()

    def log_hello(self, body, authorized):
        c = self.c
        body = body if isinstance(body, dict) else {}
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if authorized and body.get("status") == "ready":
            hooks = ", ".join(str(h) for h in body.get("hooks", []))
            print(f"\n{c(DIM)}{stamp}{c(RESET)} {c(GREEN)}{c(BOLD)}ADAPTER CONNECTED{c(RESET)} "
                  f"{body.get('adapter')} prompts={body.get('prompts')} directory={body.get('directory')}\n"
                  f"  hooks: {hooks}", file=self.out, flush=True)
        else:
            reason = str(body.get("reason", ""))[:300]
            print(f"\n{c(DIM)}{stamp}{c(RESET)} {c(RED)}{c(BOLD)}ADAPTER FAILED TO START{c(RESET)} "
                  f"status={str(body.get('status'))[:20]} reason={reason or '-'}", file=self.out, flush=True)

    async def respond(self, writer, status, result):
        data = json.dumps(result).encode()
        writer.write(f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + data)
        await writer.drain()

    async def handle(self, reader, writer):
        status, result, path, body, problem = 400, {"code": "INVALID_REQUEST"}, "?", None, None
        try:
            async with asyncio.timeout(10):
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("ascii").split("\r\n")
                method, path, _ = lines[0].split(" ")
                headers = {}
                for line in lines[1:]:
                    if line:
                        key, value = line.split(":", 1)
                        headers[key.strip().lower()] = value.strip()
                size = int(headers.get("content-length", "0"))
                authorized = hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self.token)
                if path == HELLO and method == "POST" and 0 < size <= 16384:
                    # Startup handshake from the adapter's setup(). A failing setup has no usable token,
                    # so its report is accepted unauthenticated, but only status/reason are shown.
                    body = json.loads(await reader.readexactly(size))
                    status, result = (200, {"ok": True}) if authorized else (401, {"code": "UNAUTHORIZED"})
                    self.log_hello(body, authorized)
                    body = HELLO_LOGGED
                elif not authorized:
                    status, result = 401, {"code": "UNAUTHORIZED"}
                elif method != "POST" or path not in ROUTES:
                    status, result = 404, {"code": "NOT_FOUND"}
                elif not 0 < size <= BODY_LIMIT:
                    status, result = 413, {"code": "BODY_LIMIT"}
                else:
                    body = json.loads(await reader.readexactly(size))
                    if path in ("/v1/runs/bind", "/v1/tools/catalog", "/v1/tools/execute"):
                        status, result = 501, {"code": "NOT_SUPPORTED_BY_RECEIVER"}
                    else:
                        problem = check_shape(path, body)
                        if problem is not None:
                            status, result = 400, {"code": "INVALID_REQUEST", "reason": problem}
                        else:
                            status, result = 200, reply_for(path, body)
                            if size > GATEWAY_BODY_LIMIT:  # accepted here, but worth knowing
                                problem = f"body is {size} bytes; the gateway accepts at most {GATEWAY_BODY_LIMIT}"
        except TimeoutError:
            status, result = 408, {"code": "TIMEOUT"}
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            status, result = 400, {"code": "INVALID_REQUEST"}
        finally:
            self.counts[(path, status)] = self.counts.get((path, status), 0) + 1
            if body is HELLO_LOGGED:
                pass
            elif status != 401:  # do not echo unauthenticated bodies
                self.log(status, path, body, problem)
            else:
                print(f"\nPOST {path} 401 unauthorized (token mismatch)", file=self.out, flush=True)
            try:
                await self.respond(writer, status, result)
            except (ConnectionError, OSError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def serve(port, host="127.0.0.1", compact=False, color=True):
    receiver = Receiver(os.environ.get("INTERCEPT_TOKEN", ""), compact=compact, color=color)
    server = await asyncio.start_server(receiver.handle, host, port, limit=16384)
    c = receiver.c
    print(f"{c(BOLD)}intercept.service.receiver listening on http://{host}:{port}{c(RESET)}\n"
          f"{c(YELLOW)}OBSERVE-ONLY: every request is logged and ALLOWED; nothing is enforced.\n"
          f"Raw prompt text and tool arguments are printed: use synthetic data only.{c(RESET)}\n"
          f"Waiting for the OpenCode adapter: 'ADAPTER CONNECTED' appears when OpenCode loads it.", flush=True)
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--compact", action="store_true", help="one JSON line per request")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.port, compact=args.compact, color=not args.no_color and sys.stdout.isatty()))
    except ValueError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
