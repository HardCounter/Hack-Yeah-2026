"""Provider-neutral LLM access for the control layer's own model calls (e.g. the semantic judge).

    from llm import client_from_env
    client = client_from_env()                 # OpenAI or Ollama, chosen from the environment / .env
    reason = client.available()                # None when usable, else a reason code
    reply = client.chat(messages, tools=..., schema=..., max_tokens=400)

Everything uses the standard library HTTP client; no SDK dependency.
"""
from .base import ChatResponse, LLMClient, LLMUnavailable, ToolCall
from .factory import client_from_env, load_env_file

__all__ = ["ChatResponse", "LLMClient", "LLMUnavailable", "ToolCall", "client_from_env", "load_env_file"]
