"""Branch merge: the three-way merge (merge.py), turning a merge into ops, the merge base from version ancestry,
the preview / suggest / apply endpoints and the merge eval suite. No network: the LLM is faked."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import BASE, member_client, new_client, signup  # noqa: E402

from archflow import llm, merge  # noqa: E402
from archflow.evals import runner  # noqa: E402
from archflow.evals.suites import SUITES  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402


def base_state() -> ProjectState:
    state, _ = apply_ops(ProjectState(), [
        {"op": "add_node", "node": {"name": "Web", "type": "client"}},
        {"op": "add_node", "node": {"name": "App", "type": "service", "technology": "Django",
                                    "props": {"replicas": "2"}}},
        {"op": "add_node", "node": {"name": "DB", "type": "database", "technology": "PostgreSQL"}},
        {"op": "add_edge", "edge": {"id": "e-web-app", "source": "Web", "target": "App", "protocol": "REST"}},
        {"op": "add_edge", "edge": {"id": "e-app-db", "source": "App", "target": "DB"}},
        {"op": "add_requirement", "requirement": {"title": "Checkout p95 < 300 ms", "kind": "non_functional"}},
        {"op": "add_requirement", "requirement": {"title": "Customers can pay by card"}},
    ])
    return state


def branch(state: ProjectState, ops: list[dict]) -> ProjectState:
    return apply_ops(state, ops)[0] if ops else state


def three_way(ours_ops, theirs_ops, resolutions=None):
    base = base_state()
    return base, merge.merge_states(base, branch(base, ours_ops), branch(base, theirs_ops), resolutions)


def ids(result, coll="nodes"):
    return set(getattr(result.state, coll))


# ---------------------------------------------------------------- three-way merge
def test_clean_merge_takes_both_sides():
    _, r = three_way(
        [{"op": "update_node", "id": "app", "changes": {"technology": "FastAPI"}},
         {"op": "add_node", "node": {"name": "Cache", "type": "cache"}},
         {"op": "update_node", "id": "app", "changes": {"position": {"x": 10, "y": 10}}}],
        [{"op": "update_node", "id": "app", "changes": {"description": "Checkout logic"}},
         {"op": "update_node", "id": "app", "changes": {"position": {"x": 500, "y": 500}}},
         {"op": "add_node", "node": {"name": "Queue", "type": "queue"}},
         {"op": "add_edge", "edge": {"source": "App", "target": "Queue", "protocol": "AMQP"}},
         {"op": "remove_edge", "id": "e-web-app"}])
    assert r.conflicts == [] and r.unresolved == [] and r.dropped == []
    app = r.state.nodes["app"]
    assert app.technology == "FastAPI" and app.description == "Checkout logic"
    assert app.position.x == 10  # both moved it: the target's layout wins, no conflict
    assert {"cache", "queue"} <= ids(r) and "e-web-app" not in r.state.edges
    assert any(e.target == "queue" for e in r.state.edges.values())


def test_same_change_on_both_sides_is_not_a_conflict():
    ops = [{"op": "update_node", "id": "db", "changes": {"technology": "Aurora"}}]
    _, r = three_way(ops, ops)
    assert r.conflicts == [] and r.state.nodes["db"].technology == "Aurora"


def test_field_conflict_and_resolutions():
    ours = [{"op": "update_node", "id": "db", "changes": {"technology": "MySQL"}}]
    theirs = [{"op": "update_node", "id": "db", "changes": {"technology": "CockroachDB", "description": "geo"}}]
    _, r = three_way(ours, theirs)
    (c,) = r.conflicts
    assert c["id"] == "nodes/db/technology" and c["kind"] == "field" and c["name"] == "DB"
    assert (c["base"], c["ours"], c["theirs"]) == ("PostgreSQL", "MySQL", "CockroachDB")
    assert r.unresolved == ["nodes/db/technology"] and r.state.nodes["db"].technology == "MySQL"
    assert r.state.nodes["db"].description == "geo"  # the non-conflicting field still merges
    for choice, value, want in (("ours", None, "MySQL"), ("theirs", None, "CockroachDB"),
                                ("custom", "Spanner", "Spanner")):
        _, r = three_way(ours, theirs, {c["id"]: {"choice": choice, "value": value}})
        assert r.unresolved == [] and r.state.nodes["db"].technology == want
    with pytest.raises(merge.MergeError):
        three_way(ours, theirs, {c["id"]: {"choice": "both"}})


def test_props_merge_by_key_and_lists_as_sets():
    _, r = three_way(
        [{"op": "update_node", "id": "app", "changes": {"props": {"replicas": "3", "region": "eu"},
                                                          "requirement_ids": ["REQ-001"]}}],
        [{"op": "update_node", "id": "app", "changes": {"props": {"replicas": "4", "tier": "gold"},
                                                          "requirement_ids": ["REQ-002"]}}])
    (c,) = r.conflicts
    assert c["id"] == "nodes/app/props.replicas" and (c["ours"], c["theirs"]) == ("3", "4")
    assert r.state.nodes["app"].props == {"replicas": "3", "region": "eu", "tier": "gold"}
    assert r.state.nodes["app"].requirement_ids == ["REQ-001", "REQ-002"]


def test_delete_vs_modify_conflicts():
    # we deleted the DB, they changed it
    _, r = three_way([{"op": "remove_node", "id": "db"}],
                     [{"op": "update_node", "id": "db", "changes": {"technology": "Aurora"}}])
    (c,) = r.conflicts
    assert c["id"] == "nodes/db" and c["kind"] == "deleted-in-ours" and c["ours"] is None
    assert "db" not in ids(r)  # unresolved keeps ours (deleted)
    _, r = three_way([{"op": "remove_node", "id": "db"}],
                     [{"op": "update_node", "id": "db", "changes": {"technology": "Aurora"}}],
                     {"nodes/db": {"choice": "theirs"}})
    assert r.state.nodes["db"].technology == "Aurora" and "e-app-db" in r.state.edges  # their edge comes back
    # they deleted it, we changed it
    _, r = three_way([{"op": "update_node", "id": "db", "changes": {"technology": "Aurora"}}],
                     [{"op": "remove_node", "id": "db"}], {"nodes/db": {"choice": "theirs"}})
    assert r.conflicts[0]["kind"] == "deleted-in-theirs" and "db" not in ids(r) and "e-app-db" not in r.state.edges
    # a deletion against an untouched (or merely moved) element is not a conflict
    _, r = three_way([{"op": "update_node", "id": "db", "changes": {"position": {"x": 9, "y": 9}}}],
                     [{"op": "remove_node", "id": "db"}])
    assert r.conflicts == [] and "db" not in ids(r)
    # custom is not a resolution for a deletion
    with pytest.raises(merge.MergeError):
        three_way([{"op": "remove_node", "id": "db"}],
                  [{"op": "update_node", "id": "db", "changes": {"technology": "Aurora"}}],
                  {"nodes/db": {"choice": "custom", "value": "x"}})


def test_dangling_edges_and_orphans_are_dropped():
    _, r = three_way(
        [{"op": "remove_node", "id": "db"}],
        [{"op": "add_node", "node": {"name": "Replica", "type": "database"}},
         {"op": "add_edge", "edge": {"source": "DB", "target": "Replica", "protocol": "WAL"}},
         {"op": "add_node", "node": {"name": "Orders Table", "type": "table", "parent_id": "db"}}])
    assert r.conflicts == []
    assert "db" not in ids(r) and "orders-table" not in ids(r) and "replica" in ids(r)
    dropped = {(d["entity"], d["reason"]) for d in r.dropped}
    assert ("connection", "a component it connects was removed") in dropped
    assert ("component", "the component it belongs to was removed") in dropped
    assert all(e.source in r.state.nodes and e.target in r.state.nodes for e in r.state.edges.values())


def test_duplicate_connection_from_both_sides_is_kept_once():
    _, r = three_way([{"op": "add_edge", "edge": {"source": "Web", "target": "DB", "protocol": "SQL"}}],
                     [{"op": "add_edge", "edge": {"source": "Web", "target": "DB", "protocol": "SQL"}}])
    assert sum(1 for e in r.state.edges.values() if (e.source, e.target) == ("web", "db")) == 1
    assert r.dropped and r.dropped[0]["reason"] == "the same connection already exists"


def test_requirement_merges():
    _, r = three_way(
        [{"op": "update_requirement", "id": "REQ-001", "changes": {"priority": "critical",
                                                                   "acceptance_criteria": ["p95 < 300 ms"]}},
         {"op": "add_requirement", "requirement": {"title": "Admins export orders"}},
         {"op": "update_node", "id": "app", "changes": {"requirement_ids": ["REQ-003"]}}],
        [{"op": "update_requirement", "id": "REQ-001", "changes": {"category": "performance",
                                                                   "acceptance_criteria": ["Load test in CI"]}},
         {"op": "add_requirement", "requirement": {"title": "Customers get e-mail receipts"}},
         {"op": "update_node", "id": "db", "changes": {"requirement_ids": ["REQ-003"]}},
         {"op": "remove_requirement", "id": "REQ-002"}])
    assert r.conflicts == []
    req = r.state.requirements["REQ-001"]
    assert (req.priority, req.category) == ("critical", "performance")
    assert req.acceptance_criteria == ["p95 < 300 ms", "Load test in CI"]
    assert r.state.requirements["REQ-002"].deleted  # a soft delete stays a soft delete (restorable)
    # both added REQ-003 with different titles: theirs is renumbered and its links follow
    assert r.renumbered == [{"from": "REQ-003", "to": "REQ-004", "title": "Customers get e-mail receipts"}]
    assert r.state.requirements["REQ-003"].title == "Admins export orders"
    assert r.state.requirements["REQ-004"].title == "Customers get e-mail receipts"
    assert r.state.nodes["app"].requirement_ids == ["REQ-003"] and r.state.nodes["db"].requirement_ids == ["REQ-004"]


def test_kept_requirement_keeps_its_links():
    base = base_state()
    base = branch(base, [{"op": "update_node", "id": "app", "changes": {"requirement_ids": ["REQ-002"]}}])
    ours = branch(base, [{"op": "update_requirement", "id": "REQ-002", "changes": {"priority": "high"}}])
    theirs = branch(base, [{"op": "remove_requirement", "id": "REQ-002"}])
    r = merge.merge_states(base, ours, theirs, {"requirements/REQ-002": {"choice": "ours"}})
    assert r.conflicts[0]["kind"] == "deleted-in-theirs"
    assert r.state.requirements["REQ-002"].priority == "high" and r.state.nodes["app"].requirement_ids == ["REQ-002"]


def test_soft_delete_merges_and_applies_as_a_restorable_delete():
    base = branch(base_state(), [{"op": "update_node", "id": "app", "changes": {"requirement_ids": ["REQ-002"]}}])
    ours = branch(base, [{"op": "update_node", "id": "db", "changes": {"technology": "Aurora"}}])
    theirs = branch(base, [{"op": "remove_requirement", "id": "REQ-002"}])
    r = merge.merge_states(base, ours, theirs)
    assert r.conflicts == [] and r.state.requirements["REQ-002"].deleted
    applied, _ = apply_ops(ours, merge.plan_ops(ours, r.state))
    assert applied.requirements["REQ-002"].deleted and applied.nodes["app"].requirement_ids == ["REQ-002"]
    # and a requirement deleted on the target comes back when the merge keeps the other side's change
    again = merge.merge_states(base, theirs, branch(base, [{"op": "update_requirement", "id": "REQ-002",
                                                            "changes": {"priority": "high"}}]),
                               {"requirements/REQ-002": {"choice": "theirs"}})
    restored, _ = apply_ops(theirs, merge.plan_ops(theirs, again.state))
    assert not restored.requirements["REQ-002"].deleted and restored.requirements["REQ-002"].priority == "high"


# ---------------------------------------------------------------- merged state → ops
def test_plan_ops_reproduces_the_merge():
    base = base_state()
    ours = branch(base, [{"op": "add_node", "node": {"name": "Cache", "type": "cache"}},
                         {"op": "remove_requirement", "id": "REQ-002"}])
    theirs = branch(base, [
        {"op": "remove_node", "id": "web"},
        {"op": "add_node", "node": {"name": "Orders Table", "type": "table", "parent_id": "db"}},
        {"op": "add_node", "node": {"name": "Ledger", "type": "component", "parent_id": "app"}},
        {"op": "add_node", "node": {"name": "Ledger Table", "type": "table", "parent_id": "app"}},
        {"op": "add_edge", "edge": {"source": "Ledger", "target": "Ledger Table"}},
        {"op": "update_edge", "id": "e-app-db", "changes": {"protocol": "SQL"}},
        {"op": "update_requirement", "id": "REQ-001", "changes": {"priority": "high"}}])
    r = merge.merge_states(base, ours, theirs)
    assert r.conflicts == []
    after, summary = apply_ops(ours, merge.plan_ops(ours, r.state))
    assert summary and merge.plan_ops(after, r.state) == []
    assert set(after.nodes) == set(r.state.nodes) and set(after.edges) == set(r.state.edges)
    assert after.edges["e-app-db"].protocol == "SQL" and after.requirements["REQ-001"].priority == "high"
    assert after.nodes["orders-table"].parent_id == "db"


def test_plan_ops_moves_a_connected_component_to_another_diagram():
    cur = base_state()
    new = cur.model_copy(deep=True)
    new.nodes["db"].parent_id = "app"
    del new.edges["e-app-db"]
    new.edges["e-web-app"].protocol = "gRPC"
    after, _ = apply_ops(cur, merge.plan_ops(cur, new))
    assert after.nodes["db"].parent_id == "app" and "e-app-db" not in after.edges
    assert after.edges["e-web-app"].protocol == "gRPC"


# ---------------------------------------------------------------- merge base
def test_merge_base_follows_versions_and_merge_points():
    store = Store(":memory:")
    org = store.create_org("Org")
    pid = store.create_project("P", org_id=org["id"])["id"]
    s0 = base_state()
    store.save_state(pid, "main", s0)
    v1 = store.commit(pid, "main", "start", "nk", s0)["number"]
    store.create_branch(pid, "feat", s0, v1)
    assert merge.find_base(store, pid, "feat", "main")[:1] == (v1,)  # a branch with no versions: its branch point
    s1 = branch(s0, [{"op": "update_node", "id": "db", "changes": {"technology": "Aurora"}}])
    v2 = store.commit(pid, "feat", "aurora", "nk", s1)["number"]
    v3 = store.commit(pid, "main", "main work", "nk", s0)["number"]
    n, state, how = merge.find_base(store, pid, "feat", "main")
    assert (n, how) == (v1, "common-ancestor") and state.nodes["db"].technology == "PostgreSQL"
    # after a recorded merge, the merged source version is the new base
    store.commit(pid, "main", merge.merge_message("feat", v2, "main"), "nk", s1)
    assert v3 > v2 and merge.find_base(store, pid, "feat", "main")[:2] == (v2, s1)
    # no shared history at all: the source's branch point, else an empty project
    store.create_branch(pid, "orphan", ProjectState(), None)
    assert merge.find_base(store, pid, "orphan", "main")[2] == "none"


# ---------------------------------------------------------------- REST API
@pytest.fixture()
def app():
    return create_app(Store(":memory:"))


@pytest.fixture()
def client(app):
    with new_client(app, base_url=BASE) as c:
        signup(c)
        yield c


def ops(client, pid, branch, body):
    r = client.post(f"/api/projects/{pid}/ops", params={"branch": branch}, json={"ops": body})
    assert r.status_code == 200, r.text


def make_project(client) -> str:
    pid = client.post("/api/projects", json={"name": "Shop", "template": "blank"}).json()["id"]
    ops(client, pid, "main", [
        {"op": "add_node", "node": {"name": "App", "type": "service", "technology": "Django"}},
        {"op": "add_node", "node": {"name": "DB", "type": "database", "technology": "PostgreSQL"}},
        {"op": "add_edge", "edge": {"source": "App", "target": "DB"}},
        {"op": "add_requirement", "requirement": {"title": "Checkout p95 < 300 ms", "kind": "non_functional"}}])
    assert client.post(f"/api/projects/{pid}/commit", json={"message": "start"}).status_code == 201
    assert client.post(f"/api/projects/{pid}/branches", json={"name": "feat/cache"}).status_code == 201
    ops(client, pid, "feat/cache", [
        {"op": "add_node", "node": {"name": "Cache", "type": "cache", "technology": "Redis"}},
        {"op": "add_edge", "edge": {"source": "App", "target": "Cache"}},
        {"op": "update_node", "id": "db", "changes": {"technology": "Aurora"}}])
    ops(client, pid, "main", [{"op": "update_node", "id": "db", "changes": {"technology": "MySQL"}},
                              {"op": "update_node", "id": "app", "changes": {"description": "Checkout"}}])
    return pid


def test_merge_preview_suggest_and_apply(client, monkeypatch):
    pid = make_project(client)
    url = f"/api/projects/{pid}/merge"
    body = {"source": "feat/cache", "target": "main"}
    r = client.post(f"{url}/preview", json=body)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["base"]["how"] == "common-ancestor" and p["base"]["version"] == 1
    assert [c["id"] for c in p["conflicts"]] == ["nodes/db/technology"]
    assert [x["name"] for x in p["changes"]["nodes"]["added"]] == ["Cache"]
    assert len(p["changes"]["edges"]["added"]) == 1 and "Cache" in p["changes"]["text"]

    # AI help: offline → no suggestion; with a model, one per conflict (unknown ids are dropped)
    assert client.post(f"{url}/suggest", json=body).json() == {"suggestions": [], "engine": "none"}
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", lambda system, messages, schema, **kw: {"resolutions": [
        {"id": "nodes/db/technology", "choice": "theirs", "rationale": "Aurora meets the p95 NFR (REQ-001)."},
        {"id": "nodes/nope/x", "choice": "ours", "rationale": "?"}]})
    s = client.post(f"{url}/suggest", json=body).json()
    assert s["engine"] == "llm" and s["suggestions"] == [
        {"id": "nodes/db/technology", "choice": "theirs", "rationale": "Aurora meets the p95 NFR (REQ-001)."}]

    # unresolved, stale and bad requests are refused
    apply = {**body, "source_rev": p["source_rev"], "target_rev": p["target_rev"]}
    assert client.post(f"{url}/apply", json=apply).status_code == 422
    assert client.post(f"{url}/apply", json={**apply, "target_rev": 0, "resolutions": [
        {"id": "nodes/db/technology", "choice": "theirs"}]}).status_code == 409
    assert client.post(f"{url}/preview", json={"source": "main", "target": "main"}).status_code == 422
    assert client.post(f"{url}/preview", json={"source": "nope", "target": "main"}).status_code == 404

    r = client.post(f"{url}/apply", json={**apply, "resolutions": [
        {"id": "nodes/db/technology", "choice": "custom", "value": "Aurora PostgreSQL"}]})
    assert r.status_code == 200, r.text
    out = r.json()
    main = client.get(f"/api/projects/{pid}").json()
    st = main["state"]
    assert st["nodes"]["db"]["technology"] == "Aurora PostgreSQL" and "cache" in st["nodes"]
    assert st["nodes"]["app"]["description"] == "Checkout" and len(st["edges"]) == 2
    assert main["head_version"] == out["version"] and out["safety_version"] == out["version"] - 1
    versions = {v["number"]: v for v in client.get(f"/api/projects/{pid}/versions").json()}
    assert versions[out["version"]]["message"] == f"Merged feat/cache (v{out['source_version']}) into main"
    assert versions[out["safety_version"]]["message"] == "Before merging feat/cache"
    assert versions[out["source_version"]]["branch"] == "feat/cache"  # the source as merged was snapshotted
    acts = client.get(f"/api/projects/{pid}/activity").json()
    assert acts[0]["actor"].endswith("(merge from feat/cache)") and any("Cache" in x for x in acts[0]["summary"])

    # merging again finds nothing new: the merge point is the new base
    again = client.post(f"{url}/preview", json=body).json()
    assert again["conflicts"] == [] and again["changes"]["total_changes"] == 0
    assert again["base"]["version"] == out["source_version"]


def test_merge_needs_developer(app, client):
    pid = make_project(client)
    viewer = member_client(app, client, "reviewer", "rev@example.com")
    body = {"source": "feat/cache", "target": "main"}
    for path in ("preview", "suggest", "apply"):
        assert viewer.post(f"/api/projects/{pid}/merge/{path}", json=body).status_code == 403


# ---------------------------------------------------------------- eval suite
def test_merge_eval_suite(monkeypatch):
    cases = runner.load_suites(runner.DATASETS_DIR, ["merge"])["merge"]
    case = next(c for c in cases if c["id"] == "security-nfr-takes-mtls")
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", lambda system, messages, schema, **kw: {"resolutions": [
        {"id": "nodes/payments-service/props.auth", "choice": "theirs",
         "rationale": "REQ-001 requires mutually authenticated traffic, so mTLS."}]})
    out = SUITES["merge"][0](case, None)
    assert all(ok for _, ok, _ in out.checks), out.checks
