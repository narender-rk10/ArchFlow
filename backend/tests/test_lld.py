"""HLD → LLD drill-down: diagram-level rules, offline LLD generation, the API endpoints, views and MCP tools."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import new_client, signup  # noqa: E402

from archflow import collab_sync, lld, llm  # noqa: E402
from archflow.analysis import full_analysis  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import OpError, apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402

from test_mcp_remote import BASE, oauth_login, tool  # noqa: E402


def _base() -> ProjectState:
    state, _ = apply_ops(ProjectState(), [
        {"op": "add_node", "node": {"id": "svc", "name": "Order Service"}},
        {"op": "add_node", "node": {"id": "db", "name": "Orders DB", "type": "database"}},
        {"op": "add_edge", "edge": {"source": "svc", "target": "db"}},
        {"op": "add_node", "node": {"id": "ctl", "name": "OrderController", "type": "component", "parent_id": "svc"}},
        {"op": "add_node", "node": {"id": "ep", "name": "GET /orders", "type": "endpoint", "parent_id": "Order Service",
                                    "props": {"method": "GET", "path": "/orders"}}},
        {"op": "add_edge", "edge": {"source": "ep", "target": "ctl"}},
        {"op": "add_node", "node": {"id": "deep", "name": "Mapper", "type": "component", "parent_id": "ctl"}},
    ])
    return state


# --------------------------------------------------------------------------- model rules
def test_parent_is_resolved_and_validated():
    st = _base()
    assert st.nodes["ep"].parent_id == "svc" and st.nodes["svc"].parent_id == ""
    with pytest.raises(OpError, match="parent component"):
        apply_ops(st, [{"op": "add_node", "node": {"name": "X", "parent_id": "missing"}}])
    with pytest.raises(OpError, match="inside itself"):
        apply_ops(st, [{"op": "update_node", "id": "ctl", "changes": {"parent_id": "deep"}}])
    with pytest.raises(OpError, match="has connections"):
        apply_ops(st, [{"op": "update_node", "id": "ep", "changes": {"parent_id": ""}}])


def test_cross_diagram_edge_rejected():
    st = _base()
    with pytest.raises(OpError, match="different diagrams"):
        apply_ops(st, [{"op": "add_edge", "edge": {"source": "ep", "target": "db"}}])
    edge_id = next(e.id for e in st.edges.values() if e.source == "ep")
    with pytest.raises(OpError, match="different diagrams"):
        apply_ops(st, [{"op": "update_edge", "id": edge_id, "changes": {"target": "db"}}])


def test_cascade_delete():
    st, summary = apply_ops(_base(), [{"op": "remove_node", "id": "svc"}])
    assert set(st.nodes) == {"db"} and not st.edges
    assert "3 internal element(s)" in summary[0]


def test_sanitize_drops_cross_diagram_edges_and_dangling_parents():
    raw = {"nodes": {"a": {"name": "A"}, "b": {"name": "B", "parent_id": "a"}, "c": {"name": "C", "parent_id": "gone"}},
           "edges": {"x": {"source": "a", "target": "b"}}}
    st, dropped = collab_sync.sanitize(raw)
    assert "edges/x" in dropped and st.nodes["c"].parent_id == "" and st.nodes["b"].parent_id == "a"


def test_analysis_is_per_diagram():
    st = build_template("ai-customer-support")
    base_issues = {i["code"] for i in full_analysis(st)["architecture_issues"]}
    # an unconnected LLD element is judged inside its own diagram, and never as a system-level problem
    st, _ = apply_ops(st, [
        {"op": "add_node", "node": {"id": "t1", "name": "orders", "type": "table", "parent_id": "orders-db"}},
        {"op": "add_node", "node": {"id": "c1", "name": "Ctl", "type": "component", "parent_id": "order-service"}},
        {"op": "add_node", "node": {"id": "c2", "name": "Svc", "type": "component", "parent_id": "order-service"}},
    ])
    a = full_analysis(st)
    orphans = [i for i in a["architecture_issues"] if i["code"] == "orphan"]
    assert {i["node_ids"][0] for i in orphans} == {"c1", "c2"}
    assert all(i["severity"] == "info" for i in orphans)
    assert {i["code"] for i in a["architecture_issues"]} - {"orphan"} == base_issues - {"orphan"}
    assert a["health"]["counts"]["components"] == 10 and a["health"]["counts"]["lld_elements"] == 3
    assert not {"t1", "c1", "c2"} & set(a["traceability"]["unjustified_nodes"])


# --------------------------------------------------------------------------- offline generation
def test_offline_lld_for_service():
    st = build_template("ai-customer-support")
    ops, info = lld.build(st, "Order Service")
    assert info["engine"] == "offline"
    st2, _ = apply_ops(st, ops)
    kids = [n for n in st2.nodes.values() if n.parent_id == "order-service"]
    types = [n.type for n in kids]
    assert types.count("component") >= 3 and 3 <= types.count("endpoint") <= 5 and types.count("table") == 1
    eps = [n for n in kids if n.type == "endpoint"]
    assert all(n.props["method"] in lld.HTTP_METHODS and n.props["path"].startswith("/orders") for n in eps)
    assert any("REQ-003" in n.requirement_ids for n in eps)  # endpoint inferred from a linked requirement
    table = next(n for n in kids if n.type == "table")
    assert table.name == "orders" and "id uuid pk" in table.props["columns"]
    # every generated connection stays inside the component's diagram
    kid_ids = {n.id for n in kids}
    new_edges = [e for e in st2.edges.values() if e.id not in st.edges]
    assert new_edges and all(e.source in kid_ids and e.target in kid_ids for e in new_edges)
    # laid out in columns: endpoints left of components, tables on the right
    assert max(n.position.x for n in eps) < min(n.position.x for n in kids if n.type == "component")
    assert table.position.x > max(n.position.x for n in kids if n.type == "component")
    # regenerating needs replace=True
    with pytest.raises(lld.LLDError):
        lld.build(st2, "order-service")
    ops2, _ = lld.build(st2, "order-service", replace=True)
    st3, _ = apply_ops(st2, ops2)
    assert len([n for n in st3.nodes.values() if n.parent_id == "order-service"]) == len(kids)


def test_offline_lld_for_database_and_queue_events():
    st = build_template("ai-customer-support")
    ops, _ = lld.build(st, "orders-db")
    st, _ = apply_ops(st, ops)
    tables = [n for n in st.nodes.values() if n.parent_id == "orders-db"]
    assert tables and all(n.type == "table" for n in tables)
    assert "orders" in {t.name for t in tables} and all(t.props.get("columns") for t in tables)

    st, _ = apply_ops(st, [{"op": "add_node", "node": {"id": "bus", "name": "Order Events", "type": "queue"}},
                           {"op": "add_edge", "edge": {"source": "order-service", "target": "bus"}}])
    ops, _ = lld.build(st, "order-service")
    st, _ = apply_ops(st, ops)
    kids = [n for n in st.nodes.values() if n.parent_id == "order-service"]
    assert {"OrderCreated", "OrderUpdated"} <= {n.name for n in kids if n.type == "event"}
    assert not any(n.type == "table" for n in kids)  # the database's own design already holds the tables
    v = lld.views(st, st.nodes["order-service"])
    assert {t["name"] for t in v["er"]["tables"]} == {t.name for t in tables}  # shown via the database


def test_llm_generator_used_and_falls_back(monkeypatch):
    st = build_template("ai-customer-support")
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "elements": [{"key": "c", "name": "OrderHandler", "type": "component"},
                     {"key": "e", "name": "POST /orders", "type": "endpoint", "method": "POST", "path": "/orders"},
                     {"key": "bad", "name": "Nope", "type": "service"}],
        "connections": [{"from": "e", "to": "c"}, {"from": "e", "to": "missing"}]})
    ops, info = lld.build(st, "order-service")
    assert info["engine"] == "llm" and info["counts"] == {"component": 1, "endpoint": 1} and info["connections"] == 1

    def boom(*a, **k):
        raise llm.LLMUnavailable("down")
    monkeypatch.setattr(llm, "complete_json", boom)
    _, info = lld.build(st, "order-service")
    assert info["engine"] == "offline"


# --------------------------------------------------------------------------- API
@pytest.fixture()
def client():
    with new_client(create_app(Store(":memory:")), base_url=BASE) as c:
        signup(c)
        yield c


def test_lld_endpoint_and_views(client):
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/nodes/order-service/lld", json={"actor": "nk"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "offline" and body["counts"]["endpoint"] >= 3 and body["summary"]
    state = client.get(f"/api/projects/{pid}").json()["state"]
    assert any(n["parent_id"] == "order-service" for n in state["nodes"].values())
    assert "low-level design" in client.get(f"/api/projects/{pid}/activity").json()[0]["actor"]

    assert client.post(f"/api/projects/{pid}/nodes/order-service/lld", json={"actor": "nk"}).status_code == 409
    assert client.post(f"/api/projects/{pid}/nodes/order-service/lld", json={"actor": "nk", "replace": True}).status_code == 200
    assert client.post(f"/api/projects/{pid}/nodes/nope/lld", json={}).status_code == 404

    v = client.get(f"/api/projects/{pid}/nodes/order-service/views").json()
    assert v["has_lld"] and {"GET", "POST"} <= {e["method"] for e in v["api"]["endpoints"]}
    assert v["api"]["openapi"].startswith("openapi: 3.1.0") and "/orders:" in v["api"]["openapi"]
    assert v["er"]["tables"][0]["columns"][0] == {"name": "id", "type": "uuid", "key": "PK"}
    assert v["er"]["mermaid"].startswith("erDiagram") and "uuid id PK" in v["er"]["mermaid"]
    seq = v["sequence"]["mermaid"]
    assert seq.startswith("sequenceDiagram")
    assert "actor " in seq and "API_Gateway" in seq and "Order_Service->>Orders_DB" in seq

    # cross-diagram connection through the ops API is rejected with a clear message
    ep = next(n["id"] for n in state["nodes"].values() if n["type"] == "endpoint")
    r = client.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "add_edge", "edge": {"source": ep, "target": "orders-db"}}]})
    assert r.status_code == 422 and "different diagrams" in r.json()["detail"]
    # deleting the HLD component removes its whole internal diagram
    client.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "remove_node", "id": "order-service"}]})
    state = client.get(f"/api/projects/{pid}").json()["state"]
    assert not any(n["parent_id"] == "order-service" for n in state["nodes"].values())


def test_views_without_lld(client):
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    v = client.get(f"/api/projects/{pid}/nodes/support-agent/views").json()
    assert not v["has_lld"] and v["api"]["endpoints"] == [] and v["er"]["mermaid"] == ""
    assert "Support_Agent->>Claude" in v["sequence"]["mermaid"]


def test_mcp_generate_lld(client):
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    _, tokens = oauth_login(client)
    at = tokens["access_token"]
    err, out, text = tool(client, at, "generate_lld", project_id=pid, component="Orders DB")
    assert not err, text
    assert out["engine"] == "offline" and out["counts"]["table"] >= 1 and out["ok"]
    err, out, _ = tool(client, at, "get_architecture", project_id=pid)
    tables = [c for c in out["components"] if c["parent_id"] == "orders-db"]
    assert tables and all(c["type"] == "table" and c["props"]["columns"] for c in tables)
    err, _, text = tool(client, at, "generate_lld", project_id=pid, component="Orders DB")
    assert err and "replace" in text
    err, _, text = tool(client, at, "generate_lld", project_id=pid, component="Nope")
    assert err and "not found" in text
    err, out, _ = tool(client, at, "get_component_views", project_id=pid, component="orders-db")
    assert not err and out["er"]["mermaid"].startswith("erDiagram")
    err, out, text = tool(client, at, "add_component", project_id=pid, name="Cache Adapter", type="component",
                          parent="Order Service")
    assert not err, text
    err, _, text = tool(client, at, "connect_components", project_id=pid, source="Cache Adapter", target="Orders DB")
    assert err and "different diagrams" in text

    _, ro = oauth_login(client, access="read")
    err, _, text = tool(client, ro["access_token"], "generate_lld", project_id=pid, component="order-service")
    assert err and "read-only" in text
