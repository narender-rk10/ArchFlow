"""AI settings: providers and models chosen per org (LiteLLM ids), endpoint checks, connection test."""

import pytest
from authhelp import member_client, new_client, signup

from archflow import ai_providers, llm
from archflow.main import create_app
from archflow.store import Store

TEST_SECRET = "test-only-secret-key-0123456789abcdef"


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("ARCHFLOW_SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("ARCHFLOW_AI", "auto")
    monkeypatch.setenv("ARCHFLOW_ALLOW_PRIVATE_AI_URLS", "true")
    for k in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
              "OPENROUTER_API_KEY", "ARCHFLOW_MODEL_REASONING", "ARCHFLOW_MODEL_FAST"):
        monkeypatch.setenv(k, "")  # empty, not deleted: importing LiteLLM loads backend/.env, which never overrides
    llm._reset()
    app = create_app(Store(":memory:"))
    with new_client(app) as c:
        signup(c)
        yield app, c
    llm._reset()


@pytest.fixture()
def public_dns(monkeypatch):
    """Endpoint checks resolve host names; answer with a public address instead of using the network."""
    monkeypatch.setattr(ai_providers.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("20.0.0.1", 443))])


def test_normalize_config(public_dns):
    cfg = ai_providers.normalize_config({
        "reasoning": [{"provider": "openrouter", "model": "anthropic/claude-opus-5"},
                      {"provider": "openrouter", "model": "anthropic/claude-opus-5"}],  # duplicate dropped
        "fast": [], "params": {"azure": {"api_base": "https://x.openai.azure.com", "api_version": "2025-04-01-preview",
                                          "junk": "x"}, "nope": {}}})
    assert cfg["reasoning"] == [{"provider": "openrouter", "model": "anthropic/claude-opus-5"}] and cfg["fast"] == []
    assert cfg["params"] == {"azure": {"api_base": "https://x.openai.azure.com", "api_version": "2025-04-01-preview"}}
    for bad in ({"reasoning": [{"provider": "acme", "model": "m"}]},
                {"reasoning": [{"provider": "openai", "model": "has space"}]},
                {"reasoning": [{"provider": "azure", "model": "dep"}]}):  # azure needs endpoint + version
        with pytest.raises(ValueError):
            ai_providers.normalize_config(bad)
    assert ai_providers.litellm_model("openai_compatible", "llama3.3") == "openai/llama3.3"
    assert ai_providers.litellm_model("openrouter", "openrouter/x/y") == "openrouter/x/y"


def test_endpoint_guard(monkeypatch):
    monkeypatch.setenv("ARCHFLOW_ALLOW_PRIVATE_AI_URLS", "false")
    for bad in ("ftp://x", "http://169.254.169.254/latest", "http://metadata.google.internal", "http://127.0.0.1:11434/v1"):
        with pytest.raises(ValueError):
            ai_providers.check_api_base(bad)
    monkeypatch.setenv("ARCHFLOW_ALLOW_PRIVATE_AI_URLS", "true")
    assert ai_providers.check_api_base("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434/v1"
    with pytest.raises(ValueError):
        ai_providers.check_api_base("http://169.254.169.254")  # link-local is never allowed


def test_org_models_drive_the_router(env, monkeypatch):
    app, owner = env
    org = "org-default"
    built = []

    class FakeRouter:
        def __init__(self, model_list, **kw):
            self.params = [m["litellm_params"] for m in model_list]
            built.append(self)

        def completion(self, model, fallbacks, **kw):
            from test_llm import response
            return response("OK", model=self.params[0]["model"])

    monkeypatch.setattr(llm, "_build_router", lambda model_list, fallbacks: FakeRouter(model_list))
    assert not llm.available(org)  # nothing configured anywhere
    r = owner.put(f"/api/orgs/{org}/ai-settings", json={"openrouter": "or-test-key-1234"})
    assert r.json()["providers"]["openrouter"]["last4"] == "1234"
    r = owner.put(f"/api/orgs/{org}/ai-config", json={
        "reasoning": [{"provider": "openrouter", "model": "anthropic/claude-opus-5"},
                      {"provider": "openai_compatible", "model": "llama3.3"}],
        "fast": [{"provider": "openai_compatible", "model": "qwen3:8b"}],
        "params": {"openai_compatible": {"api_base": "http://127.0.0.1:11434/v1"}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["effective"]["reasoning"] == ["openrouter/anthropic/claude-opus-5", "openai/llama3.3"]
    assert body["effective"]["fast"] == ["openai/qwen3:8b"] and body["available"] is True
    test = owner.post(f"/api/orgs/{org}/ai-test", json={"group": "reasoning"}).json()
    assert test["ok"] and test["reply"] == "OK" and test["chain"][0] == "openrouter/anthropic/claude-opus-5"
    params = {p["model"]: p for p in built[-1].params}
    assert params["openrouter/anthropic/claude-opus-5"]["api_key"] == "or-test-key-1234"
    assert params["openai/llama3.3"]["api_base"] == "http://127.0.0.1:11434/v1"
    assert params["openai/llama3.3"]["api_key"] == ai_providers.NO_KEY
    # Back to the server defaults: no env keys, so AI is off again.
    r1 = owner.put(f"/api/orgs/{org}/ai-config", json={"reasoning": [], "fast": []})
    r2 = owner.put(f"/api/orgs/{org}/ai-settings", json={"openrouter": None})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r2.json()["effective"] == {"reasoning": [], "fast": []}, r2.json()["effective"]
    assert not llm.available(org)
    assert owner.post(f"/api/orgs/{org}/ai-test", json={"group": "fast"}).json()["ok"] is False


def test_ai_config_permissions_and_validation(env):
    app, owner = env
    dev = member_client(app, owner, "developer", "dev@example.com")
    assert dev.put("/api/orgs/org-default/ai-config", json={}).status_code == 403
    assert dev.post("/api/orgs/org-default/ai-test", json={}).status_code == 403
    r = owner.put("/api/orgs/org-default/ai-config", json={"reasoning": [{"provider": "azure", "model": "gpt"}]})
    assert r.status_code == 422 and "api_base" in r.json()["detail"]
    assert owner.put("/api/orgs/org-default/ai-settings", json={"acme": "k" * 20}).status_code == 422
