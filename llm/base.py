"""Neutral message and response types shared by every provider.

Messages are dicts in one provider-neutral shape; each client converts them to its wire format:

    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...]}     # tool_calls optional
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

Tool definitions use the OpenAI function shape ({"type": "function", "function": {...}}), which
Ollama accepts unchanged.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


class LLMUnavailable(Exception):
    """The provider cannot serve the request now. `str(e)` is a fixed reason code, never response content."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ChatResponse:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def as_message(self) -> dict:
        """The assistant turn to append to the conversation."""
        return {"role": "assistant", "content": self.content, "tool_calls": list(self.tool_calls)}


class LLMClient(Protocol):
    provider: str
    model: str

    def available(self) -> str | None:
        """None when the provider and model are usable, otherwise a short reason code."""

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
             schema: Mapping[str, Any] | None = None, temperature: float | None = 0.7,
             max_tokens: int = 400) -> ChatResponse:
        """One completion. `schema` asks for a JSON object of that JSON Schema. Raises LLMUnavailable."""


def http_json(method: str, url: str, body: Mapping[str, Any] | None = None, *,
              headers: Mapping[str, str] | None = None, timeout: float = 30.0,
              max_bytes: int = 4 * 1024 * 1024) -> dict:
    """JSON over HTTP without proxies or redirects. Errors become LLMUnavailable with a fixed code."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json", **(headers or {})}, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            payload = response.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise LLMUnavailable({400: "BAD_REQUEST", 401: "AUTH_FAILED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                              429: "RATE_LIMITED"}.get(e.code, "SERVER_ERROR" if e.code >= 500 else f"HTTP_{e.code}")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMUnavailable("UNREACHABLE") from e
    if len(payload) > max_bytes:
        raise LLMUnavailable("RESPONSE_TOO_LARGE")
    try:
        return json.loads(payload)
    except ValueError:
        raise LLMUnavailable("INVALID_RESPONSE") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
