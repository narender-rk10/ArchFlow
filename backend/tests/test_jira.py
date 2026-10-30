"""Jira Cloud push: plan building, dry runs, idempotent re-pushes, 429 retries, partial failures, the encrypted token,
the site guard and role checks. Jira is a fake behind httpx.MockTransport: no network."""

import json
import os
import re

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import httpx  # noqa: E402
import pytest  # noqa: E402

from archflow import jira, secrets_box  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402

from authhelp import authed_client, member_client  # noqa: E402

TEST_SECRET = "test-only-secret-key-0123456789abcdef"
TOKEN = "atlassian-test-token-WXYZ"
SITE = "https://acme.atlassian.net"
ORG = "org-default"


class FakeJira:
    """Just enough of Jira Cloud REST v3 for the push: issues, labels, links, statuses."""

    def __init__(self):
        self.issues: dict[str, dict] = {}
        self.links: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_create = lambda fields: False  # -> True answers 400
        self.rate_limit = 0  # the next N requests get 429

    @property
    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT") and "/search/" not in c[1] and "bulkfetch" not in c[1]]

    def creates(self):
        return [c for c in self.calls if c == ("POST", "/rest/api/3/issue")]

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        assert request.headers["authorization"].startswith("Basic ")
        if self.rate_limit:
            self.rate_limit -= 1
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"errorMessages": ["slow down"]})
        body = json.loads(request.content) if request.content else {}
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"displayName": "Jira Bot", "emailAddress": "bot@acme.com"})
        if path == "/rest/api/3/project/SHOP":
            return httpx.Response(200, json={"key": "SHOP", "name": "Shop", "components": [{"name": "Order Service"}],
                                             "issueTypes": [{"id": "10", "name": "Epic", "hierarchyLevel": 1},
                                                            {"id": "11", "name": "Story", "hierarchyLevel": 0},
                                                            {"id": "12", "name": "Subtask", "hierarchyLevel": -1,
                                                             "subtask": True}]})
        if path.startswith("/rest/api/3/project/"):
            return httpx.Response(404, json={"errorMessages": ["No project could be found"]})
        if path == "/rest/api/3/field":
            return httpx.Response(200, json=[{"id": "summary", "name": "Summary"},
                                             {"id": "customfield_10016", "name": "Story point estimate"}])
        if path == "/rest/api/3/search/jql":
            label = re.search(r'labels = "([^"]+)"', body["jql"]).group(1)
            hits = [{"key": k} for k, i in self.issues.items() if label in i["labels"]]
            return httpx.Response(200, json={"issues": hits})
        if path == "/rest/api/3/issue" and method == "POST":
            f = body["fields"]
            if self.fail_create(f):
                return httpx.Response(400, json={"errors": {"summary": "rejected by a workflow validator"}})
            key = f"SHOP-{len(self.issues) + 1}"
            self.issues[key] = {**f, "status": "To Do"}
            return httpx.Response(201, json={"id": str(len(self.issues)), "key": key})
        m = re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)", path)
        if m and method == "PUT":
            if m.group(1) not in self.issues:
                return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
            self.issues[m.group(1)].update(body["fields"])
            return httpx.Response(204)
        if path == "/rest/api/3/issueLink":
            self.links.append((body["outwardIssue"]["key"], body["inwardIssue"]["key"]))
            return httpx.Response(201)
        if path == "/rest/api/3/issue/bulkfetch":
            keys = body["issueIdsOrKeys"]
            return httpx.Response(200, json={
                "issues": [{"key": k, "fields": {"status": {"name": self.issues[k]["status"],
                                                            "statusCategory": {"key": "indeterminate"}}}}
                           for k in keys if k in self.issues],
                "issueErrors": [{"issueIdOrKey": k} for k in keys if k not in self.issues]})
        return httpx.Response(404, json={"errorMessages": [f"unexpected {method} {path}"]})


@pytest.fixture()
def fake(monkeypatch):
    f = FakeJira()
    monkeypatch.setenv("ARCHFLOW_SECRET_KEY", TEST_SECRET)
    monkeypatch.setattr(jira, "_transport", httpx.MockTransport(f.handler))
    monkeypatch.setattr(jira, "_resolve", lambda host: ["20.0.0.1"])  # a public address, without DNS
    sleeps: list[float] = []
    monkeypatch.setattr(jira, "_sleep", sleeps.append)
    f.sleeps = sleeps
    return f


@pytest.fixture()
def env(fake):
    store = Store(":memory:")
    app = create_app(store)
    owner = authed_client(app)
    dev = member_client(app, owner, "developer", "dev@example.com", "Dev")
    pid = owner.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    # low-level design inside Order Service: two sub-tasks for its story
    r = owner.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "add_node", "node": {"id": "ep-orders", "name": "GET /orders/{id}", "type": "endpoint",
                                    "parent_id": "order-service", "props": {"method": "GET", "path": "/orders/{id}"}}},
        {"op": "add_node", "node": {"id": "tbl-orders", "name": "orders", "type": "table", "parent_id": "order-service"}},
    ]})
    assert r.status_code == 200, r.text
    return {"app": app, "store": store, "owner": owner, "dev": dev, "pid": pid, "fake": fake}


def connect(client, **over):
    body = {"site_url": SITE, "email": "bot@acme.com", "api_token": TOKEN, "project_key": "SHOP", **over}
    return client.put(f"/api/orgs/{ORG}/jira", json=body)


# --------------------------------------------------------------------------- plan building (pure)
def test_plan_is_derived_from_requirements_and_architecture():
    state = build_template("ai-customer-support")
    state, _ = apply_ops(state, [
        {"op": "add_node", "node": {"id": "ep", "name": "GET /orders", "type": "endpoint", "parent_id": "order-service"}},
        {"op": "add_node", "node": {"id": "tbl", "name": "orders", "type": "table", "parent_id": "order-service"}},
        {"op": "add_requirement", "requirement": {"id": "REQ-009", "title": "Export CSV", "description": "x"}},
    ])
    plan = jira.build_plan(state, project_id="p-1", project_name="Shop", links=[])
    items = {i["ref"]: i for i in plan["items"]}
    # epics: the components that own functional requirements, plus one for untraced requirements
    assert {r for r in items if r.startswith("epic:")} == {"epic:support-agent", "epic:auth-service",
                                                           "epic:order-service", "epic:unassigned"}
    # one story per functional requirement (REQ-005 is an NFR), under its owner's epic
    assert {r for r in items if r.startswith("story:")} == {"story:REQ-001", "story:REQ-002", "story:REQ-003",
                                                            "story:REQ-004", "story:REQ-009"}
    assert items["story:REQ-003"]["parent"] == "epic:order-service"
    assert items["story:REQ-002"]["parent"] == "epic:auth-service"
    assert items["story:REQ-009"]["parent"] == "epic:unassigned"
    s = items["story:REQ-003"]
    assert s["components"] == ["Support Agent", "Order Service", "Orders MCP Server", "Orders DB"]
    assert "priority-high" in s["labels"] and jira.dedupe_label("p-1", "REQ-003") in s["labels"]
    assert s["story_points"] in jira.FIB and s["points_basis"]
    assert all(c["given"] and c["when"] and c["then"] for c in s["acceptance_criteria"])
    # the existing criterion was kept as Given/When/Then; the NFR became a quality-target criterion
    assert s["acceptance_criteria"][0]["given"] == "an authenticated customer"
    assert "REQ-005" in s["description"] and "Given an authenticated customer" in s["description"]
    # sub-tasks from Order Service's low-level design, under its story only
    subs = [i for i in plan["items"] if i["type"] == "subtask"]
    assert {i["object_id"] for i in subs} == {"REQ-003:ep", "REQ-003:tbl"}
    assert all(i["parent"] == "story:REQ-003" for i in subs)
    # Support Agent -> Orders MCP Server -> Order Service: Order Service's epic blocks Support Agent's
    links = [i for i in plan["items"] if i["type"] == "link"]
    assert [(i["blocker"], i["blocked"]) for i in links] == [("epic:order-service", "epic:support-agent")]
    assert plan["counts"] == {"create": len(plan["items"]), "update": 0, "skip": 0}
    assert plan["engine"] == "offline"


def test_plan_actions_follow_the_link_table():
    state = build_template("ai-customer-support")
    first = jira.build_plan(state, project_id="p-1", project_name="Shop", links=[])
    links = [{"object_kind": i["object_kind"], "object_id": i["object_id"], "external_key": f"K-{n}",
              "external_url": "", "content_hash": i["hash"]} for n, i in enumerate(first["items"])]
    assert jira.build_plan(state, project_id="p-1", project_name="Shop", links=links)["counts"]["skip"] == len(links)
    state, _ = apply_ops(state, [{"op": "update_requirement", "id": "REQ-004", "changes": {"title": "Hand over to a human"}}])
    again = {i["ref"]: i["action"] for i in jira.build_plan(state, project_id="p-1", project_name="Shop", links=links)["items"]}
    assert again["story:REQ-004"] == "update" and again["story:REQ-001"] == "skip" and again["epic:support-agent"] == "skip"


def test_ai_stories_are_validated_and_gaps_filled(monkeypatch):
    m = jira.Model(build_template("ai-customer-support"))
    monkeypatch.setattr(jira.llm, "available", lambda org_id=None: True)
    monkeypatch.setattr(jira, "ai_stories", lambda inputs, name, **kw: {
        "REQ-001": {"requirement_id": "REQ-001", "summary": "Answer customers on WhatsApp and web",
                    "user_story": "As a customer, I want to chat, so that I get help.",
                    "acceptance_criteria": [{"given": "a customer on WhatsApp", "when": "they write", "then": "Support Agent replies"},
                                            {"given": "", "when": "broken", "then": "dropped"}],
                    "story_points": 7, "points_basis": "4 components"}})
    stories, engine = jira.write_stories(m, "Shop", use_ai=True, org_id=None)
    assert engine == "llm"
    assert stories["REQ-001"]["summary"] == "Answer customers on WhatsApp and web"
    assert len(stories["REQ-001"]["acceptance_criteria"]) == 1  # the incomplete criterion is dropped
    assert stories["REQ-001"]["story_points"] == 8  # snapped to Fibonacci
    assert stories["REQ-002"]["points_basis"].startswith("Rule-based")  # missing from the AI: rules fill in


def test_adf_document():
    doc = jira.adf("Intro line\n\n## Heading\n- one\n- two\n---\nEnd")
    assert [b["type"] for b in doc["content"]] == ["paragraph", "heading", "bulletList", "rule", "paragraph"]
    assert len(doc["content"][2]["content"]) == 2


# --------------------------------------------------------------------------- client: retries and the site guard
def test_429_is_retried_with_retry_after_and_backoff():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"displayName": "Bot"})

    waits = []
    client = jira.JiraClient(SITE, "a@b.co", "tok", transport=httpx.MockTransport(handler), sleep=waits.append)
    assert client.myself()["displayName"] == "Bot"
    assert len(calls) == 3 and len(waits) == 2 and all(3 <= w <= 4 for w in waits)

    always = jira.JiraClient(SITE, "a@b.co", "tok", max_attempts=3, sleep=waits.append,
                             transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(jira.JiraError, match="after 3 attempts"):
        always.myself()
    # without Retry-After: exponential backoff with jitter
    assert 0.25 <= jira.backoff(0, None) <= 0.5 and 2 <= jira.backoff(3, None) <= 4


def test_errors_are_not_retried_and_creates_are_not_replayed():
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(401, json={"errorMessages": ["bad token"]})

    client = jira.JiraClient(SITE, "a@b.co", "tok", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(jira.JiraError, match="HTTP 401.*email and API token.*bad token"):
        client.myself()
    with pytest.raises(jira.JiraError, match="did not answer"):
        client.create_issue({})
    assert calls == ["GET", "POST"]


def test_site_guard(monkeypatch):
    monkeypatch.setattr(jira, "_resolve", lambda host: ["20.0.0.1"])
    assert jira.check_site_url("https://acme.atlassian.net/") == "https://acme.atlassian.net"
    for bad in ("http://acme.atlassian.net", "https://user:pw@acme.atlassian.net", "https://localhost",
                "https://10.0.0.5", "https://169.254.169.254", "https://acme.atlassian.net:8443",
                "https://metadata.google.internal", "ftp://x"):
        with pytest.raises(ValueError):
            jira.check_site_url(bad)
    monkeypatch.setattr(jira, "_resolve", lambda host: ["192.168.1.20"])
    with pytest.raises(ValueError, match="private"):
        jira.check_site_url("https://jira.corp.example")
    monkeypatch.setenv("ARCHFLOW_ALLOW_PRIVATE_JIRA_URLS", "true")
    assert jira.check_site_url("https://jira.corp.example") == "https://jira.corp.example"
    with pytest.raises(ValueError):
        jira.check_site_url("https://169.254.169.254")  # link-local never
    for key in ("shop", "S", "1AB", "TOO_LONG_KEY1"):
        if key == "shop":
            assert jira.check_project_key(key) == "SHOP"
            continue
        with pytest.raises(ValueError):
            jira.check_project_key(key)


# --------------------------------------------------------------------------- the connection (admin) and its token
def test_token_is_encrypted_and_never_returned(env):
    owner, store = env["owner"], env["store"]
    r = connect(owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] and body["last4"] == "WXYZ" and TOKEN not in r.text
    row = store.get_jira_connection(ORG)
    assert row["ciphertext"] != TOKEN and TOKEN not in row["ciphertext"]
    assert secrets_box.decrypt(row["ciphertext"], jira.token_aad(ORG)) == TOKEN
    with pytest.raises(secrets_box.SecretDecryptError):  # bound to this org
        secrets_box.decrypt(row["ciphertext"], jira.token_aad("org-other"))
    assert TOKEN not in owner.get(f"/api/orgs/{ORG}/jira").text
    # saving without a token keeps the stored one
    assert connect(owner, api_token=None, project_key="shop").json()["project_key"] == "SHOP"
    assert store.get_jira_connection(ORG)["ciphertext"] == row["ciphertext"]
    assert connect(owner, site_url="http://acme.atlassian.net").status_code == 422
    t = owner.post(f"/api/orgs/{ORG}/jira/test").json()
    assert t["ok"] and t["account"] == "Jira Bot" and t["project"]["issue_types"] == ["epic", "story", "subtask"]
    assert t["project"]["story_points"] is True
    assert owner.delete(f"/api/orgs/{ORG}/jira").status_code == 204
    assert not owner.get(f"/api/orgs/{ORG}/jira").json()["configured"]


def test_roles(env):
    app, owner, dev, pid = env["app"], env["owner"], env["dev"], env["pid"]
    reviewer = member_client(app, owner, "reviewer", "rev@example.com", "Rev")
    viewer = member_client(app, owner, "viewer", "view@example.com", "View")
    assert connect(dev).status_code == 403  # admin+ configures
    assert dev.get(f"/api/orgs/{ORG}/jira").status_code == 403
    assert dev.post(f"/api/orgs/{ORG}/jira/test").status_code == 403
    assert dev.post(f"/api/projects/{pid}/jira/plan", json={}).status_code == 409  # not connected yet
    assert connect(owner).status_code == 200
    assert dev.post(f"/api/projects/{pid}/jira/plan", json={}).status_code == 200  # developer+ previews and pushes
    for c in (reviewer, viewer):
        assert c.post(f"/api/projects/{pid}/jira/plan", json={}).status_code == 403
        assert c.post(f"/api/projects/{pid}/jira/push", json={"confirm": True}).status_code == 403
        assert c.post(f"/api/projects/{pid}/jira/refresh").status_code == 403
        assert c.get(f"/api/projects/{pid}/jira/links").status_code == 200  # everyone sees the links


# --------------------------------------------------------------------------- dry run, push, re-push
def test_dry_run_makes_no_jira_requests(env):
    owner, pid, fake = env["owner"], env["pid"], env["fake"]
    connect(owner)
    fake.calls.clear()
    plan = owner.post(f"/api/projects/{pid}/jira/plan", json={}).json()
    assert plan["dry_run"] and plan["project_key"] == "SHOP" and fake.calls == []
    assert plan["by_type"] == {"epic": 3, "story": 4, "subtask": 2, "link": 1}
    assert all(i["action"] == "create" for i in plan["items"])
    # pushing needs an explicit confirmation
    assert owner.post(f"/api/projects/{pid}/jira/push", json={}).status_code == 422
    assert fake.calls == [] and env["store"].list_external_links(pid) == []


def test_push_then_repush_is_idempotent(env):
    owner, dev, pid, fake, store = env["owner"], env["dev"], env["pid"], env["fake"], env["store"]
    connect(owner)
    r = dev.post(f"/api/projects/{pid}/jira/push", json={"confirm": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["counts"] == {"created": 10, "updated": 0, "unchanged": 0, "failed": 0}
    assert len(fake.issues) == 9 and fake.links == [("SHOP-2", "SHOP-3")]  # Order Service's epic blocks Support Agent's
    story = next(i for i in fake.issues.values() if i["summary"] == "Order lookup")
    assert story["issuetype"] == {"id": "11"} and story["parent"]["key"].startswith("SHOP-")
    assert story["components"] == [{"name": "Order Service"}]  # only components that exist in Jira
    assert "component-orders-db" in story["labels"] and story["customfield_10016"] in jira.FIB
    assert story["description"]["type"] == "doc"
    sub = next(i for i in fake.issues.values() if i["summary"] == "Table: orders")
    assert sub["issuetype"] == {"id": "12"}
    links = {(x["object_kind"], x["object_id"]): x for x in store.list_external_links(pid)}
    assert links[("requirement", "REQ-003")]["external_url"].startswith(f"{SITE}/browse/SHOP-")
    acts = owner.get(f"/api/projects/{pid}/activity").json()
    assert acts[0]["actor"] == "Dev" and "10 created" in acts[0]["summary"][0] and "SHOP" in acts[0]["summary"][0]

    # same content again: nothing written
    fake.calls.clear()
    again = dev.post(f"/api/projects/{pid}/jira/push", json={"confirm": True}).json()
    assert again["counts"] == {"created": 0, "updated": 0, "unchanged": 10, "failed": 0}
    assert fake.writes == [] and len(fake.issues) == 9

    # one requirement changes: only its story is updated, in place
    owner.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "update_requirement", "id": "REQ-004", "changes": {"title": "Hand over to a human agent"}}]})
    plan = owner.post(f"/api/projects/{pid}/jira/plan", json={}).json()
    assert [i["ref"] for i in plan["items"] if i["action"] != "skip"] == ["story:REQ-004"]
    fake.calls.clear()
    third = dev.post(f"/api/projects/{pid}/jira/push", json={"confirm": True}).json()
    assert third["counts"]["updated"] == 1 and third["counts"]["created"] == 0
    assert fake.creates() == [] and len(fake.issues) == 9
    key = links[("requirement", "REQ-004")]["external_key"]
    assert fake.issues[key]["summary"] == "Hand over to a human agent"


def test_rate_limited_push_retries_and_succeeds(env):
    owner, pid, fake = env["owner"], env["pid"], env["fake"]
    connect(owner)
    fake.rate_limit = 2
    out = owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True}).json()
    assert out["counts"]["failed"] == 0 and out["counts"]["created"] == 10
    assert len(fake.sleeps) == 2 and all(s >= 2 for s in fake.sleeps)  # honoured Retry-After: 2


def test_partial_failure_keeps_successes_and_resume_pushes_only_failures(env):
    owner, pid, fake, store = env["owner"], env["pid"], env["fake"], env["store"]
    connect(owner)
    fake.fail_create = lambda f: f["summary"] == "Order lookup"
    out = owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True}).json()
    res = {r["ref"]: r for r in out["results"]}
    assert res["story:REQ-003"]["status"] == "error" and "workflow validator" in res["story:REQ-003"]["error"]
    # its sub-tasks cannot be created without it; everything else is created and linked
    assert all(res[r]["status"] == "error" and "parent" in res[r]["error"] for r in res if r.startswith("subtask:"))
    assert out["counts"] == {"created": 7, "updated": 0, "unchanged": 0, "failed": 3}
    assert len(store.list_external_links(pid)) == 7
    assert "3 failed" in owner.get(f"/api/projects/{pid}/activity").json()[0]["summary"][0]

    fake.fail_create = lambda f: False
    fake.calls.clear()
    out = owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True}).json()
    assert out["counts"] == {"created": 3, "updated": 0, "unchanged": 7, "failed": 0}
    assert len(fake.creates()) == 3 and len(fake.issues) == 9


def test_unrecorded_issue_is_found_by_label_not_duplicated(env):
    owner, pid, fake, store = env["owner"], env["pid"], env["fake"], env["store"]
    connect(owner)
    owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True})
    # the link rows are lost (e.g. a crash after Jira created the issues): the labels find them again
    for x in store.list_external_links(pid):
        store.delete_external_link(pid, x["object_kind"], x["object_id"])
    fake.calls.clear()
    out = owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True}).json()
    assert fake.creates() == [] and len(fake.issues) == 9
    assert out["counts"]["updated"] == 9 and out["counts"]["created"] == 1  # the issue link is re-created
    assert len(store.list_external_links(pid)) == 10


def test_issue_deleted_in_jira_is_created_again(env):
    owner, pid, fake, store = env["owner"], env["pid"], env["fake"], env["store"]
    connect(owner)
    owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True})
    key = next(x["external_key"] for x in store.list_external_links(pid) if x["object_id"] == "REQ-004")
    del fake.issues[key]
    owner.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "update_requirement", "id": "REQ-004", "changes": {"priority": "high"}}]})
    out = owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True}).json()
    res = next(r for r in out["results"] if r["ref"] == "story:REQ-004")
    assert res["status"] == "ok" and res["action"] == "create" and res["key"] != key


def test_refresh_pulls_status_without_touching_requirements(env):
    owner, pid, fake = env["owner"], env["pid"], env["fake"]
    connect(owner)
    owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True})
    links = owner.get(f"/api/projects/{pid}/jira/links").json()
    assert links["connected"] and links["site_url"] == SITE
    key = next(x["external_key"] for x in links["links"] if x["object_id"] == "REQ-003")
    fake.issues[key]["status"] = "In Progress"
    before = owner.get(f"/api/projects/{pid}").json()["state"]["requirements"]["REQ-003"]["status"]
    r = owner.post(f"/api/projects/{pid}/jira/refresh").json()
    assert r["updated"] == 9
    row = next(x for x in r["links"] if x["object_id"] == "REQ-003")
    assert row["status"] == "In Progress" and row["status_category"] == "indeterminate" and row["status_checked_at"]
    assert owner.get(f"/api/projects/{pid}").json()["state"]["requirements"]["REQ-003"]["status"] == before
