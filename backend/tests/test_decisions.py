"""Architecture decisions: store (SQLite + Postgres when TEST_DATABASE_URL is set), the llm → heuristic
evaluation chain, the REST endpoints (evaluate / create / adopt) and the MCP tools. No network: the LLM is faked."""

import json
import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import new_client, signup  # noqa: E402

from archflow import evaluation, llm  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402

from test_mcp_remote import BASE, oauth_login, tool  # noqa: E402

PG_URL = os.environ.get("TEST_DATABASE_URL", "")


# ---------------------------------------------------------------- sample states
def simple_state() -> ProjectState:
    state, _ = apply_ops(ProjectState(), [
        {"op": "add_node", "node": {"name": "Web", "type": "client"}},
        {"op": "add_node", "node": {"name": "App", "type": "service", "technology": "Django"}},
        {"op": "add_node", "node": {"name": "DB", "type": "database", "technology": "PostgreSQL"}},
        {"op": "add_edge", "edge": {"source": "Web", "target": "App", "protocol": "REST"}},
        {"op": "add_edge", "edge": {"source": "App", "target": "DB"}},
        {"op": "add_requirement", "requirement": {"title": "Checkout p95 < 300 ms", "kind": "non_functional",
                                                  "category": "performance", "priority": "high"}},
    ])
    return state


def scaled_state() -> ProjectState:
    state, _ = apply_ops(simple_state(), [
        {"op": "add_node", "node": {"name": "Gateway", "type": "gateway"}},
        {"op": "add_node", "node": {"name": "Auth", "type": "security", "technology": "Keycloak"}},
        {"op": "add_node", "node": {"name": "Cache", "type": "cache", "technology": "Redis"}},
        {"op": "add_node", "node": {"name": "Events", "type": "queue", "technology": "Kafka"}},
        {"op": "update_node", "id": "app", "changes": {"props": {"replicas": "3"}}},
        {"op": "add_edge", "edge": {"source": "Gateway", "target": "App"}},
        {"op": "add_edge", "edge": {"source": "Gateway", "target": "Auth"}},
        {"op": "add_edge", "edge": {"source": "App", "target": "Cache"}},
        {"op": "add_edge", "edge": {"source": "App", "target": "Events"}},
    ])
    return state


# ---------------------------------------------------------------- store
@pytest.fixture(scope="module")
def pg_store():
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    from archflow.store_pg import PostgresStore

    s = PostgresStore(PG_URL)
    yield s
    s.close()


@pytest.fixture(params=["sqlite", "postgres"])
def store(request):
    if request.param == "sqlite":
        s = Store(":memory:")
        yield s
        s.close()
    else:
        yield request.getfixturevalue("pg_store")


def test_store_decisions_crud(store):
    org = store.create_org("ADR Org")
    pid = store.create_project("P", org_id=org["id"])["id"]
    other = store.create_project("Q", org_id=org["id"])["id"]
    assert store.list_decisions(pid) == []

    alts = [{"name": "Monolith", "branch": "draft/a", "summary": "one app", "pros": ["simple"], "cons": ["scale"]}]
    d1 = store.create_decision(pid, "  Pick a DB  ", author="nk", context="ctx", alternatives=alts,
                               chosen="Monolith", requirement_ids=["REQ-001"], node_ids=["db"])
    assert d1["id"] == "ADR-001" and d1["title"] == "Pick a DB" and d1["status"] == "proposed"
    assert d1["alternatives"] == alts and d1["requirement_ids"] == ["REQ-001"] and d1["node_ids"] == ["db"]
    assert d1["evaluation"] is None and d1["author"] == "nk" and d1["created_at"] and d1["updated_at"]

    d2 = store.create_decision(pid, "Second", evaluation={"engine": "heuristic", "scores": {"a": {"cost": 3}}})
    assert d2["id"] == "ADR-002" and d2["evaluation"]["scores"]["a"]["cost"] == 3
    assert store.create_decision(other, "Other project")["id"] == "ADR-001"  # numbering is per project
    assert [d["id"] for d in store.list_decisions(pid)] == ["ADR-001", "ADR-002"]

    up = store.update_decision(pid, "ADR-001", {"status": "accepted", "decision": "Use Postgres", "id": "hack",
                                                "node_ids": ["db", "cache"]})
    assert up["id"] == "ADR-001" and up["status"] == "accepted" and up["decision"] == "Use Postgres"
    assert up["node_ids"] == ["db", "cache"] and up["context"] == "ctx"
    assert store.get_decision(pid, "ADR-001") == up
    assert store.get_decision(pid, "ADR-404") is None
    assert store.update_decision(pid, "ADR-404", {"title": "x"}) is None
    with pytest.raises(ValueError):
        store.update_decision(pid, "ADR-001", {"status": "maybe"})
    with pytest.raises(ValueError):
        store.create_decision(pid, "   ")
    store.delete_project(pid)
    assert store.list_decisions(pid) == []


# ---------------------------------------------------------------- fallback chain
ALTS = {"draft/simple": simple_state(), "draft/scaled": scaled_state()}
REQS = evaluation.merged_requirements(None, ALTS)


def test_heuristic_engine_when_nothing_configured():
    out = evaluation.evaluate(REQS, ALTS)
    assert out["engine"] == "heuristic"
    assert set(out["scores"]) == set(ALTS) and set(out["scores"]["draft/simple"]) == set(evaluation.CRITERIA)
    s, b = out["scores"]["draft/simple"], out["scores"]["draft/scaled"]
    assert b["scalability"] > s["scalability"] and b["security"] > s["security"] and s["cost"] > b["cost"]
    assert all(1 <= v <= 5 for sc in out["scores"].values() for v in sc.values())
    assert out["recommended"] in ALTS and "no LLM configured" in out["notes"]
    assert evaluation.evaluate(REQS, ALTS) == out  # deterministic
    with pytest.raises(ValueError):
        evaluation.evaluate(REQS, {"a": simple_state()})


def test_llm_engine_then_heuristic_fallback(monkeypatch):
    seen = {}

    def fake_complete_json(system, messages, schema, **kw):
        seen["schema"], seen["group"] = schema, kw.get("group")
        row = {c: 3 for c in evaluation.CRITERIA}
        return {"recommended": "B", "rationale": "B meets the latency NFR thanks to the cache.",
                "scores": [{"alternative": "A", **row}, {"alternative": "B", **row, "performance": 5}]}

    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    out = evaluation.evaluate(REQS, ALTS)
    assert out["engine"] == "llm" and out["recommended"] == "draft/scaled"
    assert out["scores"]["draft/scaled"]["performance"] == 5 and "cache" in out["rationale"]
    assert out["notes"] == []
    assert seen["schema"]["properties"]["recommended"]["enum"] == ["A", "B"] and seen["group"] == "reasoning"

    def llm_down(*a, **kw):
        raise llm.LLMUnavailable("all tiers down")

    monkeypatch.setattr(llm, "complete_json", llm_down)
    out = evaluation.evaluate(REQS, ALTS)
    assert out["engine"] == "heuristic" and any("LLM evaluation failed" in n for n in out["notes"])


# ---------------------------------------------------------------- REST API
@pytest.fixture()
def client():
    store = Store(":memory:")
    with new_client(create_app(store), base_url=BASE) as c:
        signup(c)
        yield c


def make_project(client) -> str:
    pid = client.post("/api/projects", json={"name": "Shop", "template": "blank"}).json()["id"]
    ops = [{"op": "add_requirement", "requirement": {"title": "Handle 10k rps", "kind": "non_functional",
                                                     "category": "scalability", "priority": "critical"}},
           {"op": "add_node", "node": {"name": "Legacy", "type": "service"}}]
    assert client.post(f"/api/projects/{pid}/ops", json={"ops": ops, "actor": "nk"}).status_code == 200
    for name, st in (("draft/simple", simple_state()), ("draft/scaled", scaled_state())):
        assert client.post(f"/api/projects/{pid}/branches", json={"name": name}).status_code == 201
        ops = [{"op": "remove_node", "id": "legacy"}]
        ops += [{"op": "add_node", "node": n.model_dump(exclude={"rev", "field_revs"})} for n in st.nodes.values()]
        ops += [{"op": "add_edge", "edge": e.model_dump(exclude={"rev", "field_revs", "id"})} for e in st.edges.values()]
        r = client.post(f"/api/projects/{pid}/ops", params={"branch": name}, json={"ops": ops, "actor": "nk"})
        assert r.status_code == 200, r.text
    return pid


def test_decision_rest_crud(client):
    pid = make_project(client)
    assert client.get(f"/api/projects/{pid}/decisions").json() == []
    r = client.post(f"/api/projects/{pid}/decisions", json={
        "title": "Use Kafka for events", "context": "Order events fan out", "actor": "nk",
        "alternatives": [{"name": "Kafka"}, {"name": "RabbitMQ", "pros": ["simple"]}], "chosen": "Kafka"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["id"] == "ADR-001" and d["alternatives"][1]["pros"] == ["simple"] and d["author"] == "Owner"
    assert client.get(f"/api/projects/{pid}/decisions/ADR-001").json()["title"] == "Use Kafka for events"
    assert client.get(f"/api/projects/{pid}/decisions/ADR-999").status_code == 404
    assert client.post(f"/api/projects/{pid}/decisions", json={"title": "x", "status": "maybe"}).status_code == 422

    r = client.patch(f"/api/projects/{pid}/decisions/ADR-001", json={"status": "rejected", "actor": "lead"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["context"] == "Order events fan out"
    assert client.patch(f"/api/projects/{pid}/decisions/ADR-001", json={"status": "nope"}).status_code == 422
    acts = [a["summary"][0] for a in client.get(f"/api/projects/{pid}/activity").json()]
    assert "ADR-001 status proposed → rejected" in acts and any(a.startswith("created ADR-001") for a in acts)
    # Rejected decisions cannot be adopted; Kafka is not a branch anyway.
    assert client.post(f"/api/projects/{pid}/decisions/ADR-001/adopt", json={}).status_code == 409


def test_evaluate_create_and_adopt(client):
    pid = make_project(client)
    assert client.post(f"/api/projects/{pid}/evaluate", json={"branches": ["draft/simple"]}).status_code == 422
    assert client.post(f"/api/projects/{pid}/evaluate",
                       json={"branches": ["draft/simple", "draft/nope"]}).status_code == 404
    r = client.post(f"/api/projects/{pid}/evaluate", json={"branches": ["draft/simple", "draft/scaled"]})
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["engine"] == "heuristic" and "decision" not in ev
    assert set(ev["scores"]) == {"draft/simple", "draft/scaled"} and ev["recommended"] == "draft/scaled"
    assert client.get(f"/api/projects/{pid}/decisions").json() == []

    r = client.post(f"/api/projects/{pid}/evaluate", json={"branches": ["draft/simple", "draft/scaled"],
                                                           "create_decision": True, "actor": "nk"})
    d = r.json()["decision"]
    assert d["id"] == "ADR-001" and d["status"] == "proposed" and d["chosen"] == "draft/scaled"
    assert [a["branch"] for a in d["alternatives"]] == ["draft/simple", "draft/scaled"]
    assert d["evaluation"]["engine"] == "heuristic" and "raw" not in d["evaluation"]
    assert d["requirement_ids"] == ["REQ-001"] and "heuristic" in d["context"]

    before = client.get(f"/api/projects/{pid}").json()
    assert "legacy" in before["state"]["nodes"]
    r = client.post(f"/api/projects/{pid}/decisions/ADR-001/adopt", json={"target_branch": "main", "actor": "lead"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["decision"]["status"] == "accepted" and out["source_branch"] == "draft/scaled"
    after = client.get(f"/api/projects/{pid}").json()
    scaled = client.get(f"/api/projects/{pid}", params={"branch": "draft/scaled"}).json()["state"]
    assert set(after["state"]["nodes"]) == set(scaled["nodes"]) and "legacy" not in after["state"]["nodes"]
    assert after["state"]["rev"] > before["state"]["rev"]
    assert all(n["rev"] == after["state"]["rev"] for n in after["state"]["nodes"].values())
    assert after["head_version"] == out["version"] and out["version"] == out["safety_version"] + 1
    versions = {v["number"]: v for v in client.get(f"/api/projects/{pid}/versions").json()}
    assert versions[out["version"]]["message"] == "Adopted draft/scaled (ADR-001)"
    assert versions[out["safety_version"]]["message"].startswith("Before adopting draft/scaled")
    safety = client.get(f"/api/projects/{pid}/diff", params={"from": out["safety_version"]}).json()
    assert any(x["name"] == "Legacy" for x in safety["nodes"]["removed"])
    acts = [a["summary"][0] for a in client.get(f"/api/projects/{pid}/activity").json()]
    assert "adopted draft/scaled (ADR-001)" in acts and "ADR-001 status proposed → accepted" in acts

    # adopting into the chosen branch itself is refused; a missing target is a 404
    r = client.post(f"/api/projects/{pid}/decisions/ADR-001/adopt", json={"target_branch": "draft/scaled"})
    assert r.status_code == 422
    assert client.post(f"/api/projects/{pid}/decisions/ADR-001/adopt",
                       json={"target_branch": "nope"}).status_code == 404


# ---------------------------------------------------------------- MCP
def test_mcp_decision_tools(client):
    pid = make_project(client)
    _, tokens = oauth_login(client)
    at = tokens["access_token"]
    err, out, text = tool(client, at, "evaluate_alternatives", project_id=pid, branches=["draft/simple", "draft/scaled"])
    assert not err, text
    assert out["engine"] == "heuristic" and out["decision"]["id"] == "ADR-001"

    err, out, text = tool(client, at, "create_decision", project_id=pid, title="Use Redis for sessions",
                          alternatives=[{"name": "Redis"}, {"name": "DB sessions"}], chosen="Redis",
                          requirement_ids=["REQ-001"])
    assert not err, text
    assert out["id"] == "ADR-002" and out["author"] == "Owner via MCP"
    err, _, text = tool(client, at, "create_decision", project_id=pid, title="Bad", status="maybe")
    assert err

    err, out, text = tool(client, at, "list_decisions", project_id=pid)
    listed = out.get("result", out) if out else json.loads(text)
    assert [d["id"] for d in listed] == ["ADR-001", "ADR-002"]
    err, out, _ = tool(client, at, "get_decision", project_id=pid, decision_id="ADR-001")
    assert not err and out["evaluation"]["engine"] == "heuristic"
    err, _, text = tool(client, at, "get_decision", project_id=pid, decision_id="ADR-404")
    assert err and "not found" in text

    # read-only connections can evaluate without recording, but not create decisions
    _, ro = oauth_login(client, access="read", scope="archflow:read")
    err, _, text = tool(client, ro["access_token"], "create_decision", project_id=pid, title="x")
    assert err and "read-only" in text
    err, out, _ = tool(client, ro["access_token"], "evaluate_alternatives", project_id=pid,
                       branches=["draft/simple", "draft/scaled"], create_decision=False)
    assert not err and "decision" not in out

    # org isolation
    other = client.post("/api/orgs", json={"name": "Other"}).json()["id"]
    foreign = client.post("/api/projects", json={"name": "Secret", "org_id": other}).json()["id"]
    err, _, text = tool(client, at, "list_decisions", project_id=foreign)
    assert err and "not found" in text
