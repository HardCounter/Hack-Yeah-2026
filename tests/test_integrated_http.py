"""The OpenCode wire adapter executes through the same governed gateway."""
import asyncio
import json
from pathlib import Path
import shutil

from intercept.local import LocalService
from intercept.server import Gateway


def test_authenticated_http_adapter_persists_and_prevents_denied_tool(tmp_path):
    from simulation import agent
    import generate

    generate.build(tmp_path / "base")

    async def scenario():
        service = LocalService(bank_path=tmp_path / "base" / "bank.db", runs_dir=tmp_path / "runs",
                               app_id="APP-0001", contract_id="contract_http_demo")
        # Synthetic test credentials, never persisted.
        bearer, administrator = "x" * 32, "y" * 32
        gateway = Gateway(None, bearer, None, admin_token=administrator, service=service)
        server = await asyncio.start_server(gateway.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def request(path, payload, credential=bearer):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps(payload).encode()
            writer.write(f"POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {credential}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            result = json.loads(await reader.read())
            writer.close()
            await writer.wait_closed()
            return int(head.split()[1]), result

        try:
            code, _ = await request("/v1/runs/bind", {"session_id": "sess_http_demo", "contract_id": "contract_http_demo"})
            assert code == 401
            code, binding = await request("/v1/runs/bind", {"session_id": "sess_http_demo", "contract_id": "contract_http_demo"}, administrator)
            assert code == 200 and binding["session_id"] == "sess_http_demo"
            code, read = await request("/v1/tools/execute", {"session_id": "sess_http_demo", "call_id": "call_read",
                                                          "tool": "read_application", "arguments": {"app_id": "APP-0001"}})
            assert code == 200 and read["decision"] == "ALLOW"
            assert read["tool_result"]["application_id"] == "APP-0001"
            code, deny = await request("/v1/tools/execute", {"session_id": "sess_http_demo", "call_id": "call_deny",
                                                          "tool": "read_application", "arguments": {"app_id": "APP-0002"}})
            assert code == 200 and deny["decision"] == "BLOCK"
            assert deny["action_id"] != read["action_id"]
            events = await service.runtime.persistence.wire_session("sess_http_demo")
            assert events[-1]["status"] == "blocked"
            assert events[-1]["action_id"] == deny["action_id"]
            assert await service.runtime.store.pending_deliveries() == 0
            code, result = await request("/v1/session/finish", {"session_id": "sess_http_demo"})
            assert code == 200 and result["verification_status"] == "VERIFICATION_INCOMPLETE"
        finally:
            server.close()
            await server.wait_closed()
            await service.close()

    asyncio.run(scenario())
