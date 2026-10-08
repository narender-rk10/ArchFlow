import os

import pytest
from authhelp import authed_client, ws_headers

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

from archflow import ai  # noqa: E402
from archflow.analysis import full_analysis  # noqa: E402
from archflow.diff import diff_states  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import ConflictError, OpError, apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402
from archflow.vibe import build_prompt  # noqa: E402

ai.MODE = "offline"


def base_state() -> ProjectState:
    state, _ = apply_ops(ProjectState(), [
        {"op": "add_node", "node": {"name": "API", "type": "gateway", "position": {"x": 0, "y": 0}}},
        {"op": "add_node", "node": {"name": "Order Service", "type": "service", "position": {"x": 0, "y": 160}}},
        {"op": "add_node", "node": {"name": "Orders DB", "type": "database", "technology": "PostgreSQL",
                                    "position": {"x": 0, "y": 320}}},
        {"op": "add_edge", "edge": {"source": "API", "target": "Order Service", "protocol": "REST"}},
        {"op": "add_edge", "edge": {"source": "order-service", "target": "orders-db"}},
    ])
    return state


def test_ops_resolve_names_and_are_atomic():
    state = base_state()
    assert set(state.nodes) == {"api", "order-service", "orders-db"}
    assert len(state.edges) == 2
    with pytest.raises(OpError):
        apply_ops(state, [{"op": "add_node", "node": {"name": "Cache"}},
                          {"op": "remove_node", "id": "does-not-exist"}])
    assert "cache" not in state.nodes  # untouched


def test_remove_node_drops_edges():
    state, summary = apply_ops(base_state(), [{"op": "remove_node", "id": "Order Service"}])
    assert "order-service" not in state.nodes and not state.edges
    assert "2 connection" in summary[0]


def test_field_level_conflict_detection():
    state = base_state()
    seen = state.rev
    # User A changes technology
    state, _ = apply_ops(state, [{"op": "update_node", "id": "orders-db",
                                  "changes": {"technology": "PostgreSQL 16"}, "base_rev": seen}])
    # User B, who has not seen A's change, sets a different technology → conflict
    with pytest.raises(ConflictError) as exc:
        apply_ops(state, [{"op": "update_node", "id": "orders-db",
                           "changes": {"technology": "DynamoDB"}, "base_rev": seen}])
    c = exc.value.conflicts[0]
    assert c["theirs"] == "PostgreSQL 16" and c["yours"] == "DynamoDB"
    # ...but B renaming or moving the same node is fine
    state, _ = apply_ops(state, [{"op": "update_node", "id": "orders-db", "base_rev": seen,
                                  "changes": {"name": "Order Store", "position": {"x": 5, "y": 5}}}])
    assert state.nodes["orders-db"].name == "Order Store"
    # forcing (no base_rev) = "keep mine"
    state, _ = apply_ops(state, [{"op": "update_node", "id": "orders-db", "changes": {"technology": "DynamoDB"}}])
    assert state.nodes["orders-db"].technology == "DynamoDB"


def test_diff():
    old = base_state()
    new, _ = apply_ops(old, [
        {"op": "add_node", "node": {"name": "Redis", "type": "cache"}},
        {"op": "update_node", "id": "orders-db", "changes": {"technology": "DynamoDB"}},
        {"op": "add_requirement", "requirement": {"title": "Cache reads"}},
    ])
    d = diff_states(old, new)
    assert [x["name"] for x in d["nodes"]["added"]] == ["Redis"]
    assert d["nodes"]["changed"][0]["fields"][0] == {"field": "technology", "from": "PostgreSQL", "to": "DynamoDB"}
    assert d["requirements"]["added"][0]["id"] == "REQ-001"


def test_offline_ai_between():
    state = base_state()
    res = ai.offline_command(state, "Add Redis between Order Service and Orders DB", [])
    new, _ = apply_ops(state, res.ops)
    redis = new.nodes["redis"]
    assert redis.type == "cache" and redis.technology == "Redis"
    pairs = {(e.source, e.target) for e in new.edges.values()}
    assert ("order-service", "redis") in pairs and ("redis", "orders-db") in pairs
    assert ("order-service", "orders-db") not in pairs


def test_offline_ai_misc_commands():
    state = base_state()
    for cmd in ["change Orders DB to DynamoDB", "connect API to Orders DB via SQL", "rename API to API Gateway",
                "move Orders DB right of Order Service"]:
        res = ai.offline_command(state, cmd, [])
        assert res.ops, cmd
        state, _ = apply_ops(state, res.ops)
    assert state.nodes["orders-db"].technology == "DynamoDB"
    assert state.nodes["api"].name == "API Gateway"
    assert state.nodes["orders-db"].position.x == 320


def test_analysis_on_template():
    state = build_template("ai-customer-support")
    a = full_analysis(state)
    codes = {i["code"] for i in a["requirement_issues"]}
    assert "ambiguous" in codes  # "The system should be fast."
    assert a["health"]["counts"]["components"] == 10


def test_vibe_prompt_pulls_in_trace():
    state = build_template("ai-customer-support")
    text = build_prompt(state, "Demo", [], ["REQ-003"])
    assert "Order Service" in text and "REQ-003" in text and "External components" in text


def test_api_flow():
    client = authed_client(create_app(Store(":memory:")))
    p = client.post("/api/projects", json={"name": "Demo", "template": "ai-customer-support"}).json()
    pid = p["id"]
    data = client.get(f"/api/projects/{pid}").json()
    assert data["head_version"] == 1 and len(data["state"]["nodes"]) == 10

    r = client.post(f"/api/projects/{pid}/ai/command", json={"text": "add Redis between Order Service and Orders DB"})
    assert r.status_code == 200 and r.json()["applied"], r.json()
    client.post(f"/api/projects/{pid}/commit", json={"message": "Add cache"})
    d = client.get(f"/api/projects/{pid}/diff", params={"from": "1", "to": "2"}).json()
    assert d["nodes"]["added"][0]["name"] == "Redis"

    # conflict surfaces as HTTP 409
    rev = client.get(f"/api/projects/{pid}").json()["state"]["rev"]
    client.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "update_node", "id": "orders-db", "changes": {"technology": "MySQL"}, "base_rev": rev}]})
    r = client.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "update_node", "id": "orders-db", "changes": {"technology": "DynamoDB"}, "base_rev": rev}]})
    assert r.status_code == 409 and r.json()["conflicts"][0]["field"] == "technology"

    # branch from v1, restore
    assert client.post(f"/api/projects/{pid}/branches", json={"name": "experiment/serverless", "from_version": 1}).status_code == 201
    b = client.get(f"/api/projects/{pid}", params={"branch": "experiment/serverless"}).json()
    assert "redis" not in b["state"]["nodes"]
    client.post(f"/api/projects/{pid}/restore", json={"version": 1})
    assert "redis" not in client.get(f"/api/projects/{pid}").json()["state"]["nodes"]

    # realtime: a websocket client sees state broadcasts
    with client.websocket_connect(f"/ws/{pid}/main", headers=ws_headers(client)) as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "presence"
        client.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "add_node", "node": {"name": "Kafka"}}], "actor": "spoofed"})
        msg = ws.receive_json()
        assert msg["type"] == "state" and msg["actor"] == "Owner" and "kafka" in msg["state"]["nodes"]

    assert client.get(f"/api/projects/{pid}/activity").json()[0]["actor"] == "Owner"  # client-supplied actor ignored


def test_vibe_brief_external_list_excludes_selected_components():
    """Regression: components being implemented must not also be listed as external dependencies."""
    state = build_template("ai-customer-support")
    text = build_prompt(state, "Demo", ["order-service"], [])
    external = text.split("## External components", 1)[1].split("##", 1)[0]
    assert "Order Service" not in external
    assert "Orders DB" in external or "Orders MCP Server" in external


def test_nfr_coverage_reads_wording_not_just_category():
    """Regression: a 'Compliance' NFR about GDPR/PII encryption counts for privacy and security."""
    state, _ = apply_ops(ProjectState(), [{"op": "add_requirement", "requirement": {
        "title": "EU GDPR Compliance", "kind": "non_functional", "category": "Compliance",
        "description": "Customer personal data (PII) is encrypted at rest and deleted on request.",
        "acceptance_criteria": ["Right to be forgotten handled within 30 days"]}}])
    from archflow.analysis import nfr_coverage
    assert {"privacy", "security"} <= nfr_coverage(state)
    codes = {i["code"] for i in full_analysis(state)["architecture_issues"]}
    assert "pii" not in codes


def test_shared_database_counts_every_backend_kind():
    from archflow.analysis import full_analysis

    ops = [{"op": "add_node", "node": {"name": "Core DB", "type": "database"}},
           {"op": "add_node", "node": {"name": "Orders", "type": "service"}},
           {"op": "add_node", "node": {"name": "Billing", "type": "service"}},
           {"op": "add_node", "node": {"name": "Triage", "type": "agent"}}]
    ops += [{"op": "add_edge", "edge": {"source": s, "target": "core-db"}} for s in ("orders", "billing", "triage")]
    state, _ = apply_ops(ProjectState(), ops)
    issues = [i for i in full_analysis(state)["architecture_issues"] if i["code"] == "shared_db"]
    assert len(issues) == 1 and issues[0]["severity"] == "warning" and "3 services" in issues[0]["message"]


def test_clarifying_questions_are_deduplicated():
    qs = ["For data residency compliance (EU vs India), should we deploy completely separate infrastructure stacks in "
          "AWS eu-central-1 and ap-south-1, or use a single global control plane with localized databases?",
          "How should we verify partner pharmacies?",
          "To satisfy strict data residency requirements (EU vs India), should we deploy completely isolated "
          "infrastructure silos in each region, or use a globally shared control plane with localized data persistence?",
          "How should we verify partner pharmacies?"]
    assert ai.dedupe_questions(qs) == qs[:2]


def test_guardrail_in_front_of_llm_counts_as_protection():
    ops = [{"op": "add_node", "node": {"name": "Agent", "type": "agent"}},
           {"op": "add_node", "node": {"name": "Guardrails", "type": "security"}},
           {"op": "add_node", "node": {"name": "Claude", "type": "llm"}},
           {"op": "add_edge", "edge": {"source": "agent", "target": "guardrails"}},
           {"op": "add_edge", "edge": {"source": "guardrails", "target": "claude"}}]
    state, _ = apply_ops(ProjectState(), ops)
    assert not [i for i in full_analysis(state)["architecture_issues"] if i["code"] == "llm_exposed"]


def test_new_components_do_not_land_on_existing_ones():
    state, _ = apply_ops(ProjectState(), [{"op": "add_node", "node": {"name": "API", "type": "gateway", "position": {"x": 0, "y": 0}}}])
    state, _ = apply_ops(state, [
        {"op": "add_node", "node": {"name": "Router", "type": "gateway", "position": {"x": 20, "y": 10}}},  # on top of API
        {"op": "add_node", "node": {"name": "Far", "type": "service", "position": {"x": 900, "y": 0}}},     # free: kept
        {"op": "add_node", "node": {"name": "Inner", "type": "component", "parent_id": "api", "position": {"x": 0, "y": 0}}}])
    api, router, far, inner = (state.nodes[k].position for k in ("api", "router", "far", "inner"))
    assert abs(router.x - api.x) >= 210 or abs(router.y - api.y) >= 110
    assert (far.x, far.y) == (900, 0)
    assert (inner.x, inner.y) == (0, 0)  # another diagram level: no clash
