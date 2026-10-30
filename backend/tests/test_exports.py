"""Spec exports (Markdown, print HTML, JSON, YAML), coding-agent guides (offline and agent) and their endpoints."""

import json
import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
import yaml  # noqa: E402
from authhelp import member_client, new_client, signup  # noqa: E402

from archflow import exports, lld, llm  # noqa: E402
from archflow.agents import tracing  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import Edge, Node, ProjectState, Requirement  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402

from test_mcp_remote import BASE, oauth_login, tool  # noqa: E402

DECISIONS = [
    {"id": "ADR-001", "title": "Orders go through the MCP server", "status": "accepted",
     "decision": "Support Agent reads orders only via Orders MCP Server", "node_ids": ["orders-mcp", "n-deleted"],
     "requirement_ids": ["REQ-003", "REQ-404"], "chosen": "MCP",
     "alternatives": [{"name": "MCP", "pros": ["one audited path"]}, {"name": "Direct SQL", "cons": ["couples schemas"]}]},
    {"id": "ADR-002", "title": "Cache order lookups in Redis", "status": "proposed"},
]
MEMORY = [{"text": "All customer data stays in the EU", "category": "constraint", "pinned": True}]


def support_bundle(**kw) -> exports.Bundle:
    state = build_template("ai-customer-support")
    ops, _ = lld.build(state, "Order Service")
    state, _ = apply_ops(state, ops)
    return exports.Bundle(project={"id": "p-1", "name": "Support Bot", "description": "Chat support."}, branch="main",
                          state=state, head_version=3, decisions=kw.get("decisions", DECISIONS),
                          memory=kw.get("memory", MEMORY))


def test_markdown_spec_is_derived_from_the_model_and_deterministic():
    b = support_bundle()
    md = exports.spec_markdown(b)
    assert md == exports.spec_markdown(support_bundle())
    for section in ("## 1. Overview", "## 2. Requirements", "## 3. Architecture", "## 4. Low-level designs",
                    "## 5. Architecture decisions", "## 6. Project memory", "## 7. Health"):
        assert section in md
    assert "# Support Bot: architecture specification" in md and "Branch `main` · v3" in md
    assert "#### REQ-003: Order lookup" in md and "Priority: high · Status: draft" in md
    assert "- [ ] Given an authenticated customer" in md  # acceptance criteria
    assert "Implemented by: Support Agent, Order Service, Orders MCP Server, Orders DB, Order Service › " in md
    assert "### Non-functional requirements" in md and "Category: performance" in md
    assert "#### Order Service" in md and "Technology: FastAPI" in md and "ID: `order-service`" in md
    assert "Calls: Orders DB (SQL)" in md and "Called by: Orders MCP Server (REST)" in md
    assert "### Order Service internals" in md and "| GET | `/orders/search` |" in md
    assert "### ADR-001: Orders go through the MCP server" in md and "Status: **accepted**" in md
    assert "Orders MCP Server, `n-deleted` (removed)" in md and "REQ-404 (removed)" in md
    assert "- MCP **(chosen)**" in md and "Cons: couples schemas" in md
    assert "**constraint** (pinned): All customer data stays in the EU" in md
    assert "| Architecture score |" in md and "REQ-005 uses the vague term 'fast'" in md


def test_missing_and_deleted_fields_are_handled():
    state = ProjectState(
        nodes={"a": Node(id="a", name="Lonely API", requirement_ids=["REQ-9"]),
               "child": Node(id="child", name="Orphan Module", type="component", parent_id="gone")},
        edges={"e1": Edge(id="e1", source="a", target="ghost")},
        requirements={"REQ-1": Requirement(id="REQ-1", title="Bare requirement")})
    b = exports.Bundle(project={"id": "p"}, branch="dev", state=state, decisions=[{"title": "Untitled"}], memory=[{}])
    md = exports.spec_markdown(b)
    assert "# Untitled project" in md and "no saved version yet" in md
    assert "REQ-9 (removed)" in md and "_not traced to any component_" in md
    assert "1 connection(s) point to removed components" in md
    assert "Orphan Module" in md  # its parent is gone, so it shows as a top-level component
    empty = exports.spec_markdown(exports.Bundle(project={"name": "Empty"}, branch="main", state=ProjectState()))
    assert "_No components yet._" in empty and "_No requirements recorded yet._" in empty
    assert "_No decisions recorded yet._" in empty
    files = exports.guide_files(b, use_llm=False)["files"]
    assert "Lonely API" in files["CLAUDE.md"] and "Lonely API" in files[".cursorrules"]


def test_print_html_is_light_escaped_and_paginated():
    b = support_bundle()
    b.state.nodes["web-app"].description = "<script>alert(1)</script> uses snake_case_names"
    page = exports.spec_html(b)
    assert page.startswith("<!doctype html>") and "color-scheme: light" in page and "background: #fff" in page
    assert "break-before: page" in page and "window.print()" in page
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "<table><thead><tr><th>Method</th>" in page and "<h2 class=\"first\">1. Overview</h2>" in page
    assert "snake_case_names" in page  # underscores inside words are not emphasis
    assert "☐ Given an authenticated customer" in page


def test_json_and_yaml_round_trip():
    b = support_bundle()
    text = exports.to_json(b)
    data = json.loads(text)
    assert data["schema"] == "archflow.project" and data["schema_version"] == exports.SCHEMA_VERSION
    assert data["project"]["name"] == "Support Bot" and data["head_version"] == 3
    assert [d["id"] for d in data["decisions"]] == ["ADR-001", "ADR-002"] and data["memory"][0]["pinned"] is True
    back = exports.load_document(text)
    assert back["state"] == b.state and back["decisions"] == data["decisions"]
    assert exports.load_document(exports.to_yaml(b))["state"] == b.state
    assert yaml.safe_load(exports.to_yaml(b))["state"]["nodes"]["order-service"]["technology"] == "FastAPI"
    assert exports.to_json(b) == text  # deterministic
    minimal = exports.load_document({"schema": "archflow.project", "schema_version": 1})
    assert minimal["state"] == ProjectState() and minimal["decisions"] == [] and minimal["memory"] == []
    with pytest.raises(ValueError):
        exports.load_document({"schema": "something-else", "schema_version": 1})
    with pytest.raises(ValueError):
        exports.load_document({"schema": "archflow.project", "schema_version": exports.SCHEMA_VERSION + 1})


def test_offline_guides_are_derived_skeletons(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: False)
    res = exports.guide_files(support_bundle())
    assert res["engine"] == "offline" and "No AI model" in res["notes"][0]
    claude, agents_md, rules = (res["files"][f] for f in exports.GUIDE_FILES)
    assert claude.startswith("# CLAUDE.md") and agents_md.startswith("# AGENTS.md")
    for text in (claude, agents_md, rules):
        assert "Support Bot" in text and "Order Service" in text and "FastAPI" in text
        assert "REQ-005" in text  # the NFR becomes a constraint
        rules_part = text.split(" layout")[0]  # the layout lists every ADR under docs/adr/
        assert "ADR-001" in rules_part and "ADR-002" not in rules_part  # only accepted decisions are rules
        assert "All customer data stays in the EU" in text
        assert "services/" in text and "order-service/" in text and "orderrepository/" in text  # layout from LLD
    assert "## Stack" in claude and "- PostgreSQL: Orders DB" in claude
    assert "Implements: REQ-003 Order lookup" in claude
    assert "## Conventions" not in claude  # no guidance without the agent
    assert not rules.startswith("#")


def test_agent_guide_with_a_mocked_llm(monkeypatch):
    seen = {}

    def complete_json(system, messages, schema, **kw):
        seen["prompt"] = system + "\n" + "\n".join(m["content"] for m in messages)
        return {"conventions": ["Keep Order Service routers thin; put rules in OrderService", "  "],
                "do": ["Read orders only through Orders MCP Server (ADR-001)", "Read orders only through Orders MCP Server (ADR-001)"],
                "dont": ["Do not let Support Agent open SQL connections to Orders DB"],
                "testing": [f"Test REQ-003 acceptance criteria against Order Service #{i}" for i in range(12)]}

    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", complete_json)
    with tracing.listen(lambda t: None) as traces:
        res = exports.guide_files(support_bundle())
    assert res["engine"] == "llm" and res["notes"] == []
    assert traces[-1]["prompt"] == "exports.agent_guide@v1"
    assert "Orders MCP Server" in seen["prompt"] and "REQ-005" in seen["prompt"] and "EU" in seen["prompt"]
    claude = res["files"]["CLAUDE.md"]
    assert "## Conventions\n\n- Keep Order Service routers thin" in claude and "## Don't" in claude
    assert claude.count("Read orders only through Orders MCP Server") == 1  # deduplicated
    assert claude.count("Test REQ-003 acceptance") == exports.MAX_GUIDE_ITEMS  # capped
    assert "Don't:\n- Do not let Support Agent" in res["files"][".cursorrules"]
    assert "## Order Service internals" not in claude and "## Suggested layout" in claude  # skeleton kept

    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("down")))
    res = exports.guide_files(support_bundle())
    assert res["engine"] == "offline" and "could not be written" in res["notes"][0]
    assert "## Services and their responsibilities" in res["files"]["AGENTS.md"]


def test_export_endpoints_for_viewers(monkeypatch):
    with new_client(create_app(Store(":memory:")), base_url=BASE) as owner:
        signup(owner)
        pid = owner.post("/api/projects", json={"name": "Support Bot", "template": "ai-customer-support"}).json()["id"]
        owner.post(f"/api/projects/{pid}/memory", json={"text": "All customer data stays in the EU"})
        owner.post(f"/api/projects/{pid}/decisions", json={"title": "Use Kong", "status": "accepted",
                                                            "decision": "API Gateway is Kong"})
        viewer = member_client(owner.app, owner, "viewer", "viewer@example.com")

        r = viewer.get(f"/api/projects/{pid}/export/spec.md")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
        assert 'filename="Support-Bot-main-spec.md"' in r.headers["content-disposition"]
        assert "ADR-001: Use Kong" in r.text and "All customer data stays in the EU" in r.text
        r = viewer.get(f"/api/projects/{pid}/export/spec.html", params={"inline": True})
        assert r.headers["content-disposition"].startswith("inline") and "Save as PDF" in r.text
        data = viewer.get(f"/api/projects/{pid}/export/project.json").json()
        assert data["schema_version"] == 1 and data["memory"][0]["text"] == "All customer data stays in the EU"
        assert exports.load_document(viewer.get(f"/api/projects/{pid}/export/project.yaml").text)["state"].nodes
        r = viewer.post(f"/api/projects/{pid}/export/guides", json={"use_ai": False})
        assert r.status_code == 200 and set(r.json()["files"]) == set(exports.GUIDE_FILES)
        r = viewer.get(f"/api/projects/{pid}/export/guides/cursorrules")
        assert 'filename=".cursorrules"' in r.headers["content-disposition"] and "API Gateway is Kong" in r.text
        assert viewer.get(f"/api/projects/{pid}/export/guides/readme").status_code == 404
        assert viewer.get(f"/api/projects/{pid}/export/spec.md", params={"branch": "nope"}).status_code == 404

        outsider = new_client(owner.app, base_url=BASE)
        signup(outsider, "out@example.com", "Out")
        assert outsider.get(f"/api/projects/{pid}/export/project.json").status_code in (403, 404)


def test_export_spec_mcp_tool():
    store = Store(":memory:")
    with new_client(create_app(store), base_url=BASE) as client:
        signup(client)
        pid = client.post("/api/projects", json={"name": "Support Bot", "template": "ai-customer-support"}).json()["id"]
        _, tokens = oauth_login(client, access="read", scope="archflow:read")
        at = tokens["access_token"]
        err, _, text = tool(client, at, "export_spec", project_id=pid)
        assert not err and text.startswith("# Support Bot: architecture specification") and "REQ-003" in text
        err, _, text = tool(client, at, "export_spec", project_id=pid, format="json")
        assert not err and exports.load_document(text)["state"].nodes["orders-db"].technology == "PostgreSQL"
        err, _, text = tool(client, at, "export_spec", project_id=pid, format="pdf")
        assert err and "markdown or json" in text
