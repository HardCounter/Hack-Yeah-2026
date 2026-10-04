"""Ollama native chat API (http://localhost:11434 by default)."""
from __future__ import annotations

from typing import Any, Mapping

from .base import ChatResponse, LLMUnavailable, ToolCall, http_json


class OllamaClient:
    provider = "ollama"

    def __init__(self, model: str, *, url: str = "http://localhost:11434", timeout_s: float = 60.0):
        self.model = model
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s

    def available(self) -> str | None:
        try:
            tags = http_json("GET", self.url + "/api/tags", timeout=min(self.timeout_s, 3.0))
        except LLMUnavailable:
            return "OLLAMA_UNREACHABLE"
        names = {m.get("name", "") for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            return "MODEL_NOT_PULLED"
        return None

    @staticmethod
    def _wire(message: Mapping[str, Any]) -> dict:
        if message["role"] == "assistant" and message.get("tool_calls"):
            return {"role": "assistant", "content": message.get("content") or "",
                    "tool_calls": [{"function": {"name": c.name, "arguments": dict(c.arguments)}}
                                   for c in message["tool_calls"]]}
        if message["role"] == "tool":
            return {"role": "tool", "content": message["content"], "tool_name": message.get("name", "")}
        return {"role": message["role"], "content": message.get("content") or ""}

    def chat(self, messages, *, tools=None, schema=None, temperature=0.7, max_tokens=400) -> ChatResponse:
        options: dict[str, Any] = {"num_predict": max_tokens}
        if temperature is not None:
            options["temperature"] = temperature
        body: dict[str, Any] = {"model": self.model, "messages": [self._wire(m) for m in messages],
                                "stream": False, "think": False, "options": options}
        if tools:
            body["tools"] = tools
        if schema:
            body["format"] = schema
        reply = http_json("POST", self.url + "/api/chat", body, timeout=self.timeout_s)
        message = reply.get("message") or {}
        calls = []
        for i, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            args = fn.get("arguments")
            calls.append(ToolCall(id=f"call_{i}", name=str(fn.get("name", "")),
                                  arguments=args if isinstance(args, Mapping) else {}))
        return ChatResponse(content=message.get("content") or "", tool_calls=tuple(calls),
                            input_tokens=int(reply.get("prompt_eval_count", 0)),
                            output_tokens=int(reply.get("eval_count", 0)), raw=reply)
