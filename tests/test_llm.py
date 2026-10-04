"""LLM access layer: provider selection from the environment, wire formats and failure mapping."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from llm import ChatResponse, LLMUnavailable, ToolCall, client_from_env, load_env_file
from llm.ollama import OllamaClient
from llm.openai import OpenAIClient

TOOLS = [{"type": "function", "function": {"name": "get_step", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]
SCHEMA = {"type": "object", "properties": {"p": {"type": "number", "minimum": 0, "maximum": 1},
                                           "ids": {"type": "array", "items": {"type": "string"}}}, "required": ["p"]}


# --- provider selection ----------------------------------------------------------------------

@pytest.mark.parametrize(("env", "overrides", "provider", "model"), [
    ({"OPENAI_API_KEY": "sk-test"}, None, "openai", "gpt-4.1-mini"),                 # key present -> OpenAI
    ({}, None, "ollama", "llama3.2"),                                                # nothing -> local Ollama
    ({"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-4o-mini", "OPENAI_API_KEY": "k"}, None, "openai", "gpt-4o-mini"),
    ({"LLM_PROVIDER": "ollama", "LLM_MODEL": "gemma4"}, None, "ollama", "gemma4"),
    ({"OLLAMA_MODEL": "qwen3"}, None, "ollama", "qwen3"),
    # LLM_MODEL belongs to LLM_PROVIDER: an override to ollama must not inherit an OpenAI model name
    ({"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-4.1-mini", "OPENAI_API_KEY": "k", "OLLAMA_MODEL": "gemma4"},
     {"provider": "ollama"}, "ollama", "gemma4"),
    ({"LLM_PROVIDER": "OpenAI", "OPENAI_API_KEY": "k"}, {"model": "o4-mini"}, "openai", "o4-mini"),
])
def test_provider_and_model_come_from_env_with_overrides(env, overrides, provider, model):
    client = client_from_env(overrides, env=env)
    assert (client.provider, client.model) == (provider, model)


def test_urls_and_unknown_provider():
    c = client_from_env(env={"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "http://h:1/v1/"})
    assert isinstance(c, OpenAIClient) and c.base_url == "http://h:1/v1"
    c = client_from_env({"url": "http://gpu-box:11434"}, env={"LLM_PROVIDER": "ollama"})
    assert isinstance(c, OllamaClient) and c.url == "http://gpu-box:11434"
    with pytest.raises(ValueError, match="unknown LLM provider"):
        client_from_env(env={"LLM_PROVIDER": "bard"})


def test_env_file_fills_missing_values_only(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\nLLM_PROVIDER=openai\nexport OPENAI_API_KEY='sk-file'\nLLM_MODEL=\nEMPTY\n"
                 "OLLAMA_URL=\"http://x:1\"\n")
    env = load_env_file(f, {"LLM_PROVIDER": "ollama"})
    assert env == {"LLM_PROVIDER": "ollama", "OPENAI_API_KEY": "sk-file", "OLLAMA_URL": "http://x:1"}
    assert load_env_file(tmp_path / "missing", {}) == {}


def test_api_key_is_never_shown():
    c = OpenAIClient("gpt-4.1-mini", api_key="sk-secret-value")
    assert "sk-secret" not in repr(c) and "sk-secret" not in str(vars(c).get("model"))
    assert OpenAIClient("gpt-4.1-mini", api_key=None).available() == "API_KEY_MISSING"
    with pytest.raises(LLMUnavailable, match="API_KEY_MISSING"):
        OpenAIClient("gpt-4.1-mini", api_key="").chat([{"role": "user", "content": "x"}])


# --- OpenAI wire format against a local fake server --------------------------------------------

class FakeOpenAI:
    def __init__(self, responses):
        self.responses = list(responses)          # (status, body) per request, in order
        self.requests = []
        handler = self._handler()
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append({"method": self.command, "path": self.path,
                                      "auth": self.headers.get("Authorization"), "body": body})
                status, payload = fake.responses.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _reply

            def log_message(self, *args):
                pass
        return Handler

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_openai():
    servers = []

    def make(*responses):
        servers.append(FakeOpenAI(responses))
        return servers[-1]
    yield make
    for s in servers:
        s.close()


def test_openai_tool_round_trip_and_structured_output(fake_openai):
    server = fake_openai(
        (200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_abc", "type": "function",
             "function": {"name": "get_step", "arguments": "{\"event_id\": \"e1\"}"}}]}}],
               "usage": {"prompt_tokens": 50, "completion_tokens": 7}}),
        (200, {"choices": [{"message": {"role": "assistant", "content": "{\"p\": 0.4, \"ids\": []}"}}],
               "usage": {"prompt_tokens": 80, "completion_tokens": 9}}),
    )
    client = OpenAIClient("gpt-4.1-mini", api_key="sk-test", base_url=server.url)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    first = client.chat(messages, tools=TOOLS, temperature=0.2, max_tokens=100)
    assert first.tool_calls == (ToolCall(id="call_abc", name="get_step", arguments={"event_id": "e1"}),)
    assert (first.input_tokens, first.output_tokens) == (50, 7)

    messages += [first.as_message(), {"role": "tool", "tool_call_id": "call_abc", "name": "get_step", "content": "{}"}]
    second = client.chat(messages, schema=SCHEMA, temperature=0.2, max_tokens=100)
    assert json.loads(second.content) == {"p": 0.4, "ids": []}

    req1, req2 = (r["body"] for r in server.requests)
    assert all(r["auth"] == "Bearer sk-test" and r["path"] == "/v1/chat/completions" for r in server.requests)
    assert req1["tools"] == TOOLS and req1["max_completion_tokens"] == 100 and req1["temperature"] == 0.2
    assert req2["messages"][2] == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_abc", "type": "function", "function": {"name": "get_step", "arguments": "{\"event_id\": \"e1\"}"}}]}
    assert req2["messages"][3] == {"role": "tool", "tool_call_id": "call_abc", "content": "{}"}
    fmt = req2["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["required"] == ["p", "ids"]           # strict: all properties required
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False


def test_reasoning_models_get_no_temperature(fake_openai):
    server = fake_openai((200, {"choices": [{"message": {"content": "ok"}}]}))
    OpenAIClient("o4-mini", api_key="k", base_url=server.url).chat([{"role": "user", "content": "x"}], temperature=0.7)
    assert "temperature" not in server.requests[0]["body"]


@pytest.mark.parametrize(("status", "code"), [(401, "AUTH_FAILED"), (429, "RATE_LIMITED"), (500, "SERVER_ERROR"),
                                              (400, "BAD_REQUEST")])
def test_openai_http_errors_map_to_fixed_codes(fake_openai, status, code):
    server = fake_openai((status, {"error": {"message": "secret detail from provider"}}))
    with pytest.raises(LLMUnavailable) as e:
        OpenAIClient("gpt-4.1-mini", api_key="k", base_url=server.url).chat([{"role": "user", "content": "x"}])
    assert str(e.value) == code                     # no provider text in the exception


def test_openai_availability(fake_openai):
    ok = fake_openai((200, {"id": "gpt-4.1-mini"}))
    assert OpenAIClient("gpt-4.1-mini", api_key="k", base_url=ok.url).available() is None
    assert ok.requests[0]["path"] == "/v1/models/gpt-4.1-mini"
    missing = fake_openai((404, {}))
    assert OpenAIClient("gpt-nope", api_key="k", base_url=missing.url).available() == "MODEL_NOT_FOUND"
    denied = fake_openai((401, {}))
    assert OpenAIClient("gpt-4.1-mini", api_key="bad", base_url=denied.url).available() == "AUTH_FAILED"
    assert OpenAIClient("gpt-4.1-mini", api_key="k", base_url="http://127.0.0.1:1/v1",
                        timeout_s=1).available() == "OPENAI_UNREACHABLE"


def test_malformed_openai_reply_is_unavailable(fake_openai):
    server = fake_openai((200, {"unexpected": True}))
    with pytest.raises(LLMUnavailable, match="INVALID_RESPONSE"):
        OpenAIClient("gpt-4.1-mini", api_key="k", base_url=server.url).chat([{"role": "user", "content": "x"}])


# --- Ollama message conversion --------------------------------------------------------------------

def test_ollama_wire_format_for_tool_turns():
    call = ToolCall(id="call_0", name="get_step", arguments={"event_id": "e1"})
    assert OllamaClient._wire(ChatResponse(content="", tool_calls=(call,)).as_message()) == {
        "role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_step",
                                                                          "arguments": {"event_id": "e1"}}}]}
    assert OllamaClient._wire({"role": "tool", "tool_call_id": "call_0", "name": "get_step", "content": "{}"}) == {
        "role": "tool", "content": "{}", "tool_name": "get_step"}
    assert OllamaClient("m", url="http://127.0.0.1:1", timeout_s=1).available() == "OLLAMA_UNREACHABLE"


# --- optional live checks -----------------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to call the configured provider")
def test_live_provider_round_trip():
    client = client_from_env({"timeout_s": 120})
    assert client.available() is None, f"{client.provider}/{client.model} not available"
    reply = client.chat([{"role": "user", "content": "Return JSON with p set to 0.25."}], schema=SCHEMA,
                        temperature=0, max_tokens=200)
    assert 0 <= json.loads(reply.content)["p"] <= 1
