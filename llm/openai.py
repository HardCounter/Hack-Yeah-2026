"""OpenAI Chat Completions API, or any server that implements it (OPENAI_BASE_URL)."""
from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import quote

from .base import ChatResponse, LLMUnavailable, ToolCall, http_json

# Reasoning models reject a custom temperature.
_NO_TEMPERATURE = ("o1", "o3", "o4", "gpt-5")


def _strict(schema: Mapping[str, Any]) -> dict:
    """Structured outputs in strict mode need every property required and no extra keys."""
    out = dict(schema)
    if out.get("type") == "object":
        props = {k: _strict(v) for k, v in out.get("properties", {}).items()}
        out.update(properties=props, required=list(props), additionalProperties=False)
    elif out.get("type") == "array" and isinstance(out.get("items"), Mapping):
        out["items"] = _strict(out["items"])
    return out


class OpenAIClient:
    provider = "openai"

    def __init__(self, model: str, *, api_key: str | None, base_url: str = "https://api.openai.com/v1",
                 timeout_s: float = 60.0):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._api_key = api_key or ""

    def __repr__(self) -> str:  # never show the key
        return f"OpenAIClient(model={self.model!r}, base_url={self.base_url!r})"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def available(self) -> str | None:
        if not self._api_key:
            return "API_KEY_MISSING"
        try:
            http_json("GET", f"{self.base_url}/models/{quote(self.model, safe='')}", headers=self._headers(),
                      timeout=min(self.timeout_s, 5.0))
        except LLMUnavailable as e:
            return {"NOT_FOUND": "MODEL_NOT_FOUND", "UNREACHABLE": "OPENAI_UNREACHABLE"}.get(str(e), str(e))
        return None

    @staticmethod
    def _wire(message: Mapping[str, Any]) -> dict:
        if message["role"] == "assistant" and message.get("tool_calls"):
            return {"role": "assistant", "content": message.get("content") or None,
                    "tool_calls": [{"id": c.id, "type": "function",
                                    "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))}}
                                   for c in message["tool_calls"]]}
        if message["role"] == "tool":
            return {"role": "tool", "tool_call_id": message["tool_call_id"], "content": message["content"]}
        return {"role": message["role"], "content": message.get("content") or ""}

    def chat(self, messages, *, tools=None, schema=None, temperature=0.7, max_tokens=400) -> ChatResponse:
        if not self._api_key:
            raise LLMUnavailable("API_KEY_MISSING")
        body: dict[str, Any] = {"model": self.model, "messages": [self._wire(m) for m in messages],
                                "max_completion_tokens": max_tokens}
        if temperature is not None and not self.model.startswith(_NO_TEMPERATURE):
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
        if schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "response", "strict": True, "schema": _strict(schema)}}
        reply = http_json("POST", self.base_url + "/chat/completions", body, headers=self._headers(),
                          timeout=self.timeout_s)
        try:
            message = reply["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMUnavailable("INVALID_RESPONSE") from None
        calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(id=str(call.get("id", "")), name=str(fn.get("name", "")),
                                  arguments=args if isinstance(args, Mapping) else {}))
        usage = reply.get("usage") or {}
        return ChatResponse(content=message.get("content") or "", tool_calls=tuple(calls),
                            input_tokens=int(usage.get("prompt_tokens", 0)),
                            output_tokens=int(usage.get("completion_tokens", 0)), raw=reply)
