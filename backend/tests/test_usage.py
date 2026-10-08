"""AI spend tracking and monthly budgets (no network: the LiteLLM router is faked)."""

import pytest
from authhelp import member_client, new_client, signup
from test_llm import FakeRouter, response, stream

from archflow import llm, usage
from archflow.main import create_app
from archflow.store import Store


@pytest.fixture()
def gateway(monkeypatch):
    for k in ("GEMINI_API_KEYS", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ARCHFLOW_MODEL_REASONING",
              "ARCHFLOW_MODEL_FAST", "ARCHFLOW_LLM_PRICES", "ARCHFLOW_ORG_RPM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setenv("ARCHFLOW_AI", "auto")
    llm._reset()
    store = Store(":memory:")
    org = store.create_org("Acme")["id"]
    budgets = usage.install(store)
    yield store, org, budgets
    llm.set_usage_hooks(None, None)
    llm._reset()


def fake(monkeypatch, *items):
    router = FakeRouter({"reasoning": list(items)})
    monkeypatch.setattr(llm, "_get_router", lambda *a, **k: router)
    return router


def test_feature_from_path():
    assert usage.scope_for_path("/api/projects/p-1/conversations/cv-1/ask") == {"project_id": "p-1", "feature": "assistant"}
    assert usage.scope_for_path("/api/projects/p-1/conversations/cv-1/command")["feature"] == "canvas-edit"
    assert usage.scope_for_path("/api/projects/p-1/chat")["feature"] == "design-chat"
    assert usage.scope_for_path("/api/projects/p-1/openspec/changes")["feature"] == "openspec"
    assert usage.scope_for_path("/api/projects/p-1/nodes/x/lld")["feature"] == "low-level-design"
    assert usage.scope_for_path("/mcp") == {"feature": "mcp"}
    assert usage.scope_for_path("/api/health") == {"feature": "other"}


def test_cost_estimates(monkeypatch):
    cost, source = llm.estimate_cost("gemini/gemini-3.1-pro-preview", 1_000_000, 1_000_000)
    assert source == "table" and cost == pytest.approx(14.0)
    monkeypatch.setenv("ARCHFLOW_LLM_PRICES", '{"gemini-3.1-pro": [1, 1]}')
    assert llm.estimate_cost("gemini/gemini-3.1-pro-preview", 500_000, 500_000)[0] == pytest.approx(1.0)
    assert llm.estimate_cost("some/unknown-model", 1000, 1000) == (0.0, "unknown")


def test_calls_are_recorded_with_scope(gateway, monkeypatch):
    store, org, _ = gateway
    fake(monkeypatch, response("hi"), stream("streamed ", "answer"))
    with llm.usage_scope(project_id="p-1", feature="assistant"):
        llm.complete_tools("sys", [{"role": "user", "content": "hello"}], [], org_id=org)
        assert "".join(llm.stream_text("sys", [{"role": "user", "content": "x" * 400}], org_id=org)) == "streamed answer"
    s = store.ai_usage_summary(org, "2000-01-01")
    assert s["total"]["calls"] == 2 and s["total"]["cost_usd"] > 0
    assert s["by_feature"][0]["key"] == "assistant" and s["by_project"][0]["key"] == "p-1"
    assert s["total"]["prompt_tokens"] >= 10 + 100  # 10 reported + ~400 chars / 4 estimated for the stream


def test_budget_blocks_calls_and_falls_back(gateway, monkeypatch):
    store, org, budgets = gateway
    fake(monkeypatch, response("one"), response("two"))
    llm.complete_tools("sys", [{"role": "user", "content": "hi"}], [], org_id=org)
    assert llm.available(org)
    store.set_ai_budget(org, 0.000001, 80, "admin")
    budgets.forget(org)
    assert not llm.available(org)  # features use their offline paths
    with pytest.raises(llm.LLMBudgetExceeded, match="monthly AI budget"):
        llm.complete_tools("sys", [{"role": "user", "content": "hi"}], [], org_id=org)
    assert llm.available("other-org")  # other organizations are unaffected
    store.set_ai_budget(org, None, 80, "admin")
    budgets.forget(org)
    assert llm.available(org)


# --------------------------------------------------------------------------- REST
@pytest.fixture()
def client():
    app = create_app(Store(":memory:"))
    with new_client(app) as c:
        signup(c)
        yield c


def test_usage_and_budget_endpoints(client):
    store = client.app.state.store
    org = "org-default"
    pid = client.post("/api/projects", json={"name": "Shop", "template": "blank"}).json()["id"]
    store.record_ai_usage({"org_id": org, "project_id": pid, "feature": "openspec", "model": "gemini/x",
                           "prompt_tokens": 1000, "completion_tokens": 500, "cost_usd": 0.9, "cost_source": "table"})
    r = client.get(f"/api/orgs/{org}/ai-usage").json()
    assert r["month_spend_usd"] == pytest.approx(0.9) and r["status"] == "ok" and r["used_percent"] is None
    assert r["by_project"][0]["name"] == "Shop" and r["by_feature"][0]["key"] == "openspec"
    r = client.put(f"/api/orgs/{org}/ai-budget", json={"monthly_limit_usd": 1.0, "alert_percent": 80}).json()
    assert r["status"] == "warning" and r["used_percent"] == 90.0
    r = client.put(f"/api/orgs/{org}/ai-budget", json={"monthly_limit_usd": 0.5}).json()
    assert r["status"] == "blocked"
    assert not llm.available(org)
    assert client.put(f"/api/orgs/{org}/ai-budget", json={"monthly_limit_usd": -1}).status_code == 422
    dev = member_client(client.app, client, "developer", "dev@example.com")
    assert dev.get(f"/api/orgs/{org}/ai-usage").status_code == 403
    assert dev.put(f"/api/orgs/{org}/ai-budget", json={"monthly_limit_usd": None}).status_code == 403
    client.put(f"/api/orgs/{org}/ai-budget", json={"monthly_limit_usd": None})
