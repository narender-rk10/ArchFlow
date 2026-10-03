import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

from fastapi.testclient import TestClient  # noqa: E402

from archflow import collab_sync  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402

H = {"x-internal-token": collab_sync.INTERNAL_TOKEN}


def base() -> ProjectState:
    s, _ = apply_ops(ProjectState(), [
        {"op": "add_node", "node": {"name": "API", "type": "gateway"}},
        {"op": "add_node", "node": {"name": "DB", "type": "database", "technology": "PostgreSQL"}},
        {"op": "add_edge", "edge": {"source": "api", "target": "db", "id": "e1"}},
    ])
    return s


def test_state_patch_is_field_level():
    old = base()
    new, _ = apply_ops(old, [
        {"op": "update_node", "id": "db", "changes": {"technology": "MySQL"}},
        {"op": "add_node", "node": {"name": "Cache", "type": "cache"}},
        {"op": "remove_edge", "id": "e1"},
    ])
    p = collab_sync.state_patch(old, new)
    assert p["nodes"]["set"]["db"] == {"technology": "MySQL"}  # only the changed field
    assert p["nodes"]["set"]["cache"]["name"] == "Cache"
    assert p["edges"]["del"] == ["e1"]
    assert "rev" not in p["nodes"]["set"]["cache"]
    assert collab_sync.state_patch(new, new) == {}


def test_sanitize_drops_invalid_and_dangling():
    raw = {"nodes": {"a": {"name": "A", "type": "weird"}, "b": {"type": "service"}},  # b has no name
           "edges": {"e": {"source": "a", "target": "missing"}},
           "requirements": {"REQ-001": {"title": "T", "priority": "urgent"}}}
    state, dropped = collab_sync.sanitize(raw)
    assert state.nodes["a"].type == "service"  # unknown type coerced
    assert "b" not in state.nodes and not state.edges and not state.requirements
    assert set(dropped) == {"nodes/b", "edges/e", "requirements/REQ-001"}


def test_merge_projection_bumps_only_changed_fields():
    cur = base()
    incoming = cur.model_copy(deep=True)
    incoming.nodes["db"].technology = "MySQL"
    incoming.nodes["api"].position.x = 99
    merged, summary = collab_sync.merge_projection(cur, incoming)
    assert merged.rev == cur.rev + 1
    assert merged.nodes["db"].field_revs["technology"] == merged.rev
    assert merged.nodes["db"].field_revs["name"] == cur.nodes["db"].field_revs["name"]
    assert any("moved 'API'" in s for s in summary) and any("technology" in s for s in summary)
    same, none = collab_sync.merge_projection(merged, merged)
    assert none == [] and same is merged


def test_internal_endpoints_and_live_doc_sync(monkeypatch):
    pushed = []

    async def fake_push(pid, branch, patch, actor):
        pushed.append((pid, branch, patch, actor))
        return True

    monkeypatch.setattr(collab_sync, "push_patch", fake_push)
    store = Store(":memory:")
    client = TestClient(create_app(store))
    pid = client.post("/api/projects", json={"name": "P", "template": "ai-customer-support"}).json()["id"]
    name = f"{pid}:main"

    assert client.get(f"/api/internal/ydocs/{name}").status_code == 401
    assert client.get(f"/api/internal/ydocs/{name}", headers=H).status_code == 404
    assert client.put(f"/api/internal/ydocs/{name}", content=b"\x01\x02", headers=H).status_code == 204
    assert client.get(f"/api/internal/ydocs/{name}", headers=H).content == b"\x01\x02"

    # Projection from the live doc becomes the branch state, attributed to the editors.
    state = client.get(f"/api/projects/{pid}").json()["state"]
    state["nodes"]["orders-db"]["technology"] = "CockroachDB"
    r = client.put(f"/api/internal/projection/{name}", json={"state": state, "actors": ["Priya", "Narender"]}, headers=H)
    assert r.json()["changed"] is True
    assert client.get(f"/api/projects/{pid}").json()["state"]["nodes"]["orders-db"]["technology"] == "CockroachDB"
    act = client.get(f"/api/projects/{pid}/activity").json()[0]
    assert act["actor"] == "Narender, Priya" and "technology" in act["summary"][0]
    assert client.put(f"/api/internal/projection/{name}", json={"state": state}, headers=H).json()["changed"] is False
    assert not pushed  # projections from the doc are never echoed back to it

    # Server-side edits (MCP/AI/REST ops) are pushed to the live doc as a field-level patch.
    client.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "update_node", "id": "orders-db", "changes": {"name": "Orders"}}], "actor": "mcp"})
    assert pushed[-1][2] == {"nodes": {"set": {"orders-db": {"name": "Orders"}}, "del": []}} and pushed[-1][3] == "mcp"


def test_unreachable_collab_drops_stale_doc(monkeypatch):
    async def down(*_a, **_k):
        return False

    monkeypatch.setattr(collab_sync, "push_patch", down)
    store = Store(":memory:")
    client = TestClient(create_app(store))
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    store.put_ydoc(pid, "main", b"stale")
    client.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "add_node", "node": {"name": "X"}}]})
    assert store.get_ydoc(pid, "main") is None  # rebuilt from the API state next time it is opened
