"""Interactive diagram export through ArchFlow's renderer (rendering tests need Node.js 18+)."""

import pytest
from authhelp import member_client, new_client, signup

from archflow import diagram_export as dx
from archflow import lld
from archflow.main import create_app
from archflow.models import ProjectState
from archflow.ops import apply_ops
from archflow.store import Store
from archflow.templates import build_template

needs_node = pytest.mark.skipif(not dx.available(), reason="Node.js 18+ is not installed")


def test_brand_matching():
    assert dx.brand_for("PostgreSQL") == "postgresql"
    assert dx.brand_for("Amazon RDS for PostgreSQL 16") == "postgresql"
    assert dx.brand_for("Kafka") == "apache-kafka"  # alias
    assert dx.brand_for("", "Redis cache") == "redis"  # falls back to the name
    assert dx.brand_for("Go") == "go" and dx.brand_for("Google Cloud Run") != "go"  # short keys only match exactly
    assert dx.brand_for("Kong") is None and dx.brand_for("") is None


def test_architecture_ir_maps_types_positions_and_logos():
    state = build_template("ai-customer-support")
    ir = dx.architecture_ir(state, "Support")
    comps = {c["label"]: c for c in ir["components"]}
    assert comps["Orders DB"]["type"] == "database" and comps["Orders DB"]["brand"] == "postgresql"
    assert comps["WhatsApp"]["type"] == "frontend" and comps["Claude"]["type"] == "external"
    assert min(c["pos"][0] for c in ir["components"]) == dx.MARGIN  # shifted into positive space
    gw, svc = state.nodes["api-gateway"].position, state.nodes["order-service"].position
    gap = comps["Order Service"]["pos"][0] - comps["API Gateway"]["pos"][0]
    assert gap == round(svc.x - gw.x)  # canvas layout kept
    assert all(c["id"][0].isalpha() for c in ir["components"])
    assert len(ir["connections"]) == len(state.edges)
    long = apply_ops(state, [{"op": "add_node", "node": {"name": "Managed Service for Apache Kafka", "type": "queue"}}])[0]
    kafka = next(c for c in dx.architecture_ir(long, "x")["components"] if c["label"].startswith("Managed"))
    assert kafka["size"][0] > dx.NODE_W  # widened to fit its label


def test_architecture_ir_levels_and_empty():
    with pytest.raises(dx.ExportError, match="nothing to export"):
        dx.architecture_ir(ProjectState(), "x")
    state, _ = apply_ops(ProjectState(), [
        {"op": "add_node", "node": {"name": "Svc", "type": "service"}},
        {"op": "add_node", "node": {"name": "Handler", "type": "component", "parent_id": "svc"}},
        {"op": "add_node", "node": {"name": "Note", "type": "note"}}])
    assert [c["label"] for c in dx.architecture_ir(state, "x")["components"]] == ["Svc"]
    assert [c["label"] for c in dx.architecture_ir(state, "x", "svc")["components"]] == ["Handler"]


def test_repair_labels_applies_suggestions_then_drops():
    ir = {"components": [{"id": "a", "pos": [0, 0], "size": [180, 64]}, {"id": "b", "pos": [400, 0], "size": [180, 64]},
                         {"id": "c", "pos": [0, 300], "size": [180, 64]}],
          "connections": [{"from": "a", "to": "b", "label": "REST"}, {"from": "a", "to": "c", "label": "REST"}]}
    diag = ('Architecture layout validation failed:\n'
            '- Label "REST" overlaps component "a" - adjust labelDx/labelDy/labelSegment or set labelAt.\n'
            '  label rect: [75, 170, 30, 14]\n  component "a" rect: [0, 0, 180, 64]\n'
            '  Suggested fix: set labelAt [90, 200] or set labelDy 24 (below)\n')
    assert dx.repair_labels(ir, diag)
    assert "labelAt" not in ir["connections"][0] and ir["connections"][1]["labelAt"] == [90.0, 200.0]  # nearest
    assert not dx.repair_labels(ir, diag)  # same suggestion again: nothing new
    assert dx.repair_labels(ir, diag, drop=True)
    assert "label" not in ir["connections"][1] and ir["connections"][0]["label"] == "REST"


def test_sequence_ir_from_request_flow():
    state = build_template("ai-customer-support")
    node = state.nodes["order-service"]
    flow = lld.sequence_steps(state, node, lld.api_view(state, node))
    ir = dx.sequence_ir(flow, "Order flow")
    assert [p["label"] for p in ir["participants"]][-1] == "Orders DB"
    assert ir["messages"][0]["y"] >= 160 and all(b["y"] > a["y"] for a, b in zip(ir["messages"], ir["messages"][1:]))
    assert any(m.get("variant") == "return" for m in ir["messages"])
    with pytest.raises(dx.ExportError, match="no request flow"):
        dx.sequence_ir({"participants": [{"id": "x", "name": "X", "type": "service"}], "messages": []}, "x")


@needs_node
def test_render_architecture_and_sequence():
    state = build_template("ai-customer-support")
    html, layout = dx.export_architecture(state, "Support")
    assert html.lstrip().lower().startswith("<!doctype html") and "Orders DB" in html and layout == "authored"
    node = state.nodes["order-service"]
    seq = dx.export_sequence(lld.sequence_steps(state, node, lld.api_view(state, node)), "Order flow")
    assert "Order flow" in seq


@needs_node
def test_crowded_layout_falls_back(monkeypatch):
    ops = [{"op": "add_node", "node": {"name": f"Service {i}", "type": "service"}} for i in range(4)]
    state, _ = apply_ops(ProjectState(), ops)
    for i, n in enumerate(state.nodes.values()):  # stacked on top of each other (as a hand-dragged canvas can be)
        n.position.x, n.position.y = 0, i * 20
    _, layout = dx.export_architecture(state, "Crowded")
    assert layout in ("authored-spread", "grid")


def test_export_unavailable_without_node(monkeypatch):
    monkeypatch.setattr(dx, "node_binary", lambda: None)
    with pytest.raises(dx.ExportUnavailable, match="Node.js"):
        dx.render({"diagram_type": "architecture"})


# --------------------------------------------------------------------------- REST
@pytest.fixture()
def client():
    app = create_app(Store(":memory:"))
    with new_client(app) as c:
        signup(c)
        yield c


@needs_node
def test_export_endpoints(client):
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    r = client.get(f"/api/projects/{pid}/export/diagram")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-disposition"] == 'attachment; filename="Shop-main.html"'
    r = client.get(f"/api/projects/{pid}/nodes/order-service/export/sequence")
    assert r.status_code == 200 and "Order Service" in r.text
    viewer = member_client(client.app, client, "viewer", "viewer@example.com")
    assert viewer.get(f"/api/projects/{pid}/export/diagram").status_code == 200  # read access is enough
    assert client.get(f"/api/projects/{pid}/export/diagram", params={"level": "order-service"}).status_code == 422
    assert client.get(f"/api/projects/{pid}/export/diagram", params={"level": "nope"}).status_code == 404
    assert client.get("/api/health").json()["export"] == {"interactive": True}


def test_export_endpoint_reports_missing_node(client, monkeypatch):
    monkeypatch.setattr(dx, "node_binary", lambda: None)
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    r = client.get(f"/api/projects/{pid}/export/diagram")
    assert r.status_code == 503 and "Node.js" in r.json()["detail"]
