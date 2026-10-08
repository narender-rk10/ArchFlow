"""LLM providers an organization can configure from the UI (keys in secrets_box, models in its AI config).

Every call goes through LiteLLM (llm.py), so any provider here is one LiteLLM "provider/model" id away. Each
entry says what the provider needs: an API key (required or optional), and extra parameters such as an endpoint
(``api_base``) or an ``api_version``. ``examples`` are hints for the model fields, not a fixed list: any model id
the provider accepts works.

The server's own environment keys (GEMINI_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) still apply to orgs
that configure nothing; an org's own key for a provider replaces the environment's for that org.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urlparse

PROVIDERS: dict[str, dict[str, Any]] = {
    "gemini": {"label": "Google Gemini", "prefix": "gemini/", "key": "required", "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
               "docs": "https://aistudio.google.com/apikey",
               "examples": {"reasoning": "gemini-3.1-pro-preview", "fast": "gemini-3.8-flash"}},
    "anthropic": {"label": "Anthropic Claude", "prefix": "anthropic/", "key": "required", "env": ["ANTHROPIC_API_KEY"],
                  "docs": "https://console.anthropic.com/settings/keys",
                  "examples": {"reasoning": "claude-opus-5", "fast": "claude-haiku-4-5"}},
    "openai": {"label": "OpenAI", "prefix": "openai/", "key": "required", "env": ["OPENAI_API_KEY"],
               "docs": "https://platform.openai.com/api-keys",
               "examples": {"reasoning": "gpt-5.6", "fast": "gpt-5.4-mini"}},
    "openrouter": {"label": "OpenRouter (hundreds of models, one key)", "prefix": "openrouter/", "key": "required",
                   "env": ["OPENROUTER_API_KEY"], "docs": "https://openrouter.ai/keys",
                   "examples": {"reasoning": "anthropic/claude-opus-5", "fast": "google/gemini-3.8-flash"}},
    "mistral": {"label": "Mistral", "prefix": "mistral/", "key": "required", "env": ["MISTRAL_API_KEY"],
                "docs": "https://console.mistral.ai/api-keys",
                "examples": {"reasoning": "mistral-large-latest", "fast": "mistral-small-latest"}},
    "groq": {"label": "Groq", "prefix": "groq/", "key": "required", "env": ["GROQ_API_KEY"],
             "docs": "https://console.groq.com/keys",
             "examples": {"reasoning": "openai/gpt-oss-120b", "fast": "llama-3.3-70b-versatile"}},
    "deepseek": {"label": "DeepSeek", "prefix": "deepseek/", "key": "required", "env": ["DEEPSEEK_API_KEY"],
                 "docs": "https://platform.deepseek.com/api_keys",
                 "examples": {"reasoning": "deepseek-reasoner", "fast": "deepseek-chat"}},
    "xai": {"label": "xAI Grok", "prefix": "xai/", "key": "required", "env": ["XAI_API_KEY"],
            "docs": "https://console.x.ai", "examples": {"reasoning": "grok-4", "fast": "grok-3-mini"}},
    "azure": {"label": "Azure OpenAI", "prefix": "azure/", "key": "required", "env": [],
              "params": {"api_base": "Endpoint, e.g. https://my-resource.openai.azure.com",
                         "api_version": "API version, e.g. 2025-04-01-preview"},
              "required_params": ["api_base", "api_version"], "docs": "https://portal.azure.com",
              "examples": {"reasoning": "<your deployment name>", "fast": "<your deployment name>"}},
    "openai_compatible": {"label": "OpenAI-compatible server (Ollama, vLLM, LM Studio, LiteLLM proxy…)",
                          "prefix": "openai/", "key": "optional", "env": [],
                          "params": {"api_base": "Base URL ending in /v1, e.g. http://ollama:11434/v1"},
                          "required_params": ["api_base"],
                          "examples": {"reasoning": "llama3.3", "fast": "qwen3:8b"}},
}
GROUPS = ("reasoning", "fast")
MAX_CHAIN = 4
NO_KEY = "not-needed"  # OpenAI-compatible servers that accept any key


def litellm_model(provider: str, model: str) -> str:
    prefix = PROVIDERS[provider]["prefix"]
    model = model.strip()
    return model if model.startswith(prefix) else prefix + model


def env_key(provider: str) -> str:
    return next((os.environ[e].strip() for e in PROVIDERS[provider].get("env", []) if os.environ.get(e, "").strip()), "")


def check_api_base(url: str) -> str:
    """Validate an endpoint an admin typed. Cloud metadata / link-local addresses are always refused; private and
    loopback addresses (a local Ollama) only with ARCHFLOW_ALLOW_PRIVATE_AI_URLS=true. Raises ValueError."""
    url = (url or "").strip().rstrip("/")
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("The endpoint must be an http(s) URL.")
    allow_private = os.environ.get("ARCHFLOW_ALLOW_PRIVATE_AI_URLS", "").lower() in ("1", "true", "yes")
    host = u.hostname
    if host.lower() in ("metadata.google.internal", "metadata"):
        raise ValueError("That endpoint is not allowed.")
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve {host}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError("That endpoint is not allowed (link-local or reserved address).")
        if (ip.is_private or ip.is_loopback) and not allow_private:
            raise ValueError("Private and local addresses are disabled on this server "
                             "(set ARCHFLOW_ALLOW_PRIVATE_AI_URLS=true to allow a local model server).")
    return url


def normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate an org's AI config: {"reasoning": [{provider, model}], "fast": [...], "params": {provider: {...}}}.
    An empty chain means "use the server's defaults" for that group. Raises ValueError."""
    out: dict[str, Any] = {"params": {}}
    for group in GROUPS:
        chain = []
        for entry in (raw.get(group) or [])[:MAX_CHAIN]:
            provider = str(entry.get("provider") or "")
            model = str(entry.get("model") or "").strip()
            if provider not in PROVIDERS:
                raise ValueError(f"Unknown provider {provider!r}.")
            if not model or len(model) > 200 or any(c.isspace() for c in model):
                raise ValueError(f"Enter a model id for {PROVIDERS[provider]['label']} (no spaces).")
            if {"provider": provider, "model": model} not in chain:
                chain.append({"provider": provider, "model": model})
        out[group] = chain
    used = {e["provider"] for g in GROUPS for e in out[g]}
    for provider, params in (raw.get("params") or {}).items():
        if provider not in PROVIDERS or not isinstance(params, dict):
            continue
        clean = {}
        for name in PROVIDERS[provider].get("params", {}):
            value = str(params.get(name) or "").strip()
            if value:
                clean[name] = check_api_base(value) if name == "api_base" else value[:100]
        if clean:
            out["params"][provider] = clean
    for provider in used:
        missing = [p for p in PROVIDERS[provider].get("required_params", []) if not out["params"].get(provider, {}).get(p)]
        if missing:
            raise ValueError(f"{PROVIDERS[provider]['label']} needs: {', '.join(missing)}.")
    return out


def catalog() -> list[dict[str, Any]]:
    return [{"id": k, "label": v["label"], "key": v["key"], "params": v.get("params", {}),
             "required_params": v.get("required_params", []), "docs": v.get("docs"), "examples": v["examples"],
             "server_key": bool(env_key(k))} for k, v in PROVIDERS.items()]
