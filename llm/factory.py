"""Build an LLM client from the environment (and the repository .env file).

Variables (see .env.example):
    LLM_PROVIDER     openai | ollama. Default: openai when OPENAI_API_KEY is set, else ollama.
    LLM_MODEL        model for LLM_PROVIDER. Default: gpt-4.1-mini (openai) or OLLAMA_MODEL / llama3.2 (ollama).
    OPENAI_API_KEY   required for openai; never logged.
    OPENAI_BASE_URL  optional, for OpenAI-compatible servers. Default https://api.openai.com/v1.
    OLLAMA_URL       default http://localhost:11434.
    LLM_TIMEOUT_S    per-request timeout, default 60.

Component overrides (e.g. a plugin's config block) take precedence over the environment.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from .ollama import OllamaClient
from .openai import OpenAIClient

REPO_ENV = Path(__file__).resolve().parents[1] / ".env"
PROVIDERS = ("openai", "ollama")


def load_env_file(path: str | Path = REPO_ENV, env: dict | None = None) -> dict:
    """Read KEY=VALUE lines into `env` (default os.environ) without overriding values already set."""
    env = os.environ if env is None else env
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in env:
            env[key] = value
    return env


def client_from_env(overrides: Mapping[str, object] | None = None, *, env: Mapping[str, str] | None = None,
                    env_file: str | Path | None = REPO_ENV):
    """Return an OpenAIClient or OllamaClient. Raises ValueError only for an unknown provider name."""
    if env is None:
        env = load_env_file(env_file) if env_file else os.environ
    o = {k: v for k, v in (overrides or {}).items() if v not in (None, "")}
    configured = env.get("LLM_PROVIDER") or None
    provider = str(o.get("provider") or configured or ("openai" if env.get("OPENAI_API_KEY") else "ollama")).lower()
    if provider not in PROVIDERS:
        raise ValueError(f"unknown LLM provider {provider!r}; expected one of {', '.join(PROVIDERS)}")
    # LLM_MODEL belongs to LLM_PROVIDER: do not send an OpenAI model name to Ollama or vice versa.
    env_model = env.get("LLM_MODEL") if configured in (None, provider) else None
    timeout = float(o.get("timeout_s") or env.get("LLM_TIMEOUT_S") or 60)
    if provider == "openai":
        return OpenAIClient(str(o.get("model") or env_model or "gpt-4.1-mini"), api_key=env.get("OPENAI_API_KEY"),
                            base_url=str(o.get("url") or env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"),
                            timeout_s=timeout)
    return OllamaClient(str(o.get("model") or env_model or env.get("OLLAMA_MODEL") or "llama3.2"),
                        url=str(o.get("url") or env.get("OLLAMA_URL") or "http://localhost:11434"), timeout_s=timeout)
