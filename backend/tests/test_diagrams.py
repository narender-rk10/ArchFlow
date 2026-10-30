"""Diagrams from the model (diagrams.py): C4 context/container, sequence per critical flow, ER, deployment, data flow,
the HLD itself; Mermaid / PlantUML text, escaping, alt text, the flow-naming agent, the API and the MCP tool."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import member_client, new_client, signup  # noqa: E402

from archflow import diagrams, lld, llm  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402

from test_mcp_remote import BASE, oauth_login, tool  # noqa: E402

IDS = ["hld", "c4-context", "c4-container", "sequence-1", "er", "deployment", "dataflow"]
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _state(ops) -> ProjectState:
    state, _ = apply_ops(ProjectState(), ops)
    return state


def _node(id_, name, type_="service", technology="", **kw):
    return {"op": "add_node", "node": {"id": id_, "name": name, "type": type_, "technology": technology, **kw}}


def _edge(s, t, protocol=""):
    return {"op": "add_edge", "edge": {"source": s, "target": t, "protocol": protocol}}


def _by_id(state, name="Shop", **kw):
    return {d["id"]: d for d in diagrams.all_diagrams(state, name, **kw)}


def _with_lld(state: ProjectState, *refs: str) -> ProjectState:
    for ref in refs:
        ops, _ = lld.build(state, ref)
        state, _ = apply_ops(state, ops)
    return state


@pytest.fixture(autouse=True)
def _clear_cache():
    diagrams._cache.clear()
    yield
    diagrams._cache.clear()


# --------------------------------------------------------------------------- empty and LLD-less projects
def test_empty_project_has_every_diagram_empty_with_alt_text():
    found = diagrams.all_diagrams(ProjectState(), "Empty")
    assert [d["id"] for d in found] == IDS
    for d in found:
        assert d["empty"] and d["mermaid"] == "" and d["plantuml"] == "" and d["alt"], d["id"]
    assert "no components" in found[0]["alt"].lower()
    # notes alone are not components
    assert _by_id(_state([_node("n", "Remember", "note")]))["hld"]["empty"]


def test_template_without_lld():
    d = _by_id(build_template("ai-customer-support"))
    assert [k for k in d] == IDS and not any(d[k]["empty"] for k in IDS if k != "er")
    assert d["er"]["empty"] and "low-level design" in d["er"]["alt"] and d["er"]["tables"] == 0

    hld = d["hld"]["mermaid"]
    assert hld.startswith("flowchart TB\n    accTitle: Shop: high-level architecture\n    accDescr: ")
    assert 'n_Orders_DB[("Orders DB<br/><small>[PostgreSQL]</small>")]' in hld
    assert 'n_Order_Service -->|"SQL"| n_Orders_DB' in hld and "n_WhatsApp([" in hld
    assert "10 components and 9 connections" in d["hld"]["alt"]
    pu = d["hld"]["plantuml"]
    assert pu.startswith("@startuml") and pu.endswith("@enduml")
    assert 'database "Orders DB\\n[PostgreSQL]" as n_Orders_DB' in pu and "n_Order_Service --> n_Orders_DB : SQL" in pu


def test_c4_context_and_container():
    d = _by_id(build_template("ai-customer-support"))
    ctx = d["c4-context"]
    assert 's_Shop["Shop<br/>' in ctx["mermaid"] and 'n_WhatsApp -->|"Uses: HTTPS webhook"| s_Shop' in ctx["mermaid"]
    # internal connections collapse into the system box: only client → system and system → external remain
    assert ctx["mermaid"].count("-->") == 4 and "n_Order_Service" not in ctx["mermaid"]
    assert ctx["plantuml"].splitlines()[1] == "!include <C4/C4_Context>"
    assert 'Person(n_Web_App, "Web App", "React")' in ctx["plantuml"]
    assert 'System_Ext(n_Helpdesk, "Helpdesk", "Zendesk")' in ctx["plantuml"]
    assert "Used by WhatsApp and Web App" in ctx["alt"] and "Claude and Helpdesk" in ctx["alt"]

    cont = d["c4-container"]
    lines = cont["mermaid"].splitlines()
    start, end = lines.index('    subgraph g_Shop["Shop"]'), lines.index("    end")
    inside = "\n".join(lines[start:end])
    assert "n_Order_Service" in inside and "n_Orders_DB" in inside and "n_WhatsApp" not in inside and "n_Claude" not in inside
    pu = cont["plantuml"]
    assert '!include <C4/C4_Container>' in pu and 'System_Boundary(g_Shop, "Shop") {' in pu
    assert 'ContainerDb(n_Orders_DB, "Orders DB", "PostgreSQL", "")' in pu
    assert 'Rel(n_Support_Agent, n_Helpdesk, "REST (escalation)")' in pu
    assert "Inside the system boundary" in cont["alt"]


# --------------------------------------------------------------------------- sequence
def test_sequence_traces_client_to_store_with_side_calls():
    state = build_template("ai-customer-support")
    (flow,) = diagrams.offline_flows(state)
    assert flow.name == "Order lookup" and flow.requirement_ids == ["REQ-003"]
    assert flow.path == ["whatsapp", "api-gateway", "support-agent", "orders-mcp", "order-service", "orders-db"]
    seq = _by_id(state)["sequence-1"]
    mm = seq["mermaid"]
    assert mm.startswith("sequenceDiagram") and "    autonumber" in mm
    assert "actor n_WhatsApp as WhatsApp" in mm
    assert "n_API_Gateway->>n_Auth_Service: OIDC verify token" in mm and "n_Support_Agent->>n_Claude: Messages API prompt" in mm
    assert "n_Order_Service->>n_Orders_DB: SQL query / write" in mm and "n_API_Gateway-->>n_WhatsApp: response" in mm
    assert mm.count("activate n_") == 2 * mm.count("deactivate n_")  # activate/deactivate pairs ("deactivate" contains "activate")
    # participants in order of first appearance
    parts = [line.split()[1] for line in mm.splitlines() if line.strip().startswith(("actor ", "participant "))]
    assert parts[:3] == ["n_WhatsApp", "n_API_Gateway", "n_Auth_Service"]
    pu = seq["plantuml"]
    assert 'database "Orders DB" as n_Orders_DB' in pu and 'boundary "API Gateway" as n_API_Gateway' in pu
    assert "n_Order_Service -> n_Orders_DB : SQL query / write" in pu and "n_Orders_DB --> n_Order_Service : result" in pu
    assert seq["alt"].startswith("Sequence diagram of the flow 'Order lookup', implementing REQ-003 Order lookup")
    assert seq["path"][0] == "WhatsApp" and seq["engine"] == "offline"


def test_sequence_flows_per_store_queues_and_entry_points():
    state = _state([
        {"op": "add_requirement", "requirement": {"id": "REQ-1", "title": "Place an order", "priority": "critical"}},
        _node("web", "Web", "client", requirement_ids=["REQ-1"]),
        _node("svc", "Checkout", requirement_ids=["REQ-1"]),
        _node("db", "Orders DB", "database", requirement_ids=["REQ-1"]),
        _node("q", "Events", "queue"),
        _node("worker", "Mailer"),
        _node("mdb", "Mail DB", "database"),
        _edge("web", "svc", "HTTPS"), _edge("svc", "db", "SQL"), _edge("svc", "q", "Kafka"), _edge("q", "worker"),
        _edge("worker", "mdb"),
    ])
    found = diagrams.offline_flows(state)
    assert [f.path for f in found] == [["web", "svc", "db"], ["web", "svc", "q", "worker", "mdb"]]
    # both serve REQ-1, so the names are made unique with the store
    assert [f.name for f in found] == ["Place an order (Orders DB)", "Place an order (Mail DB)"]
    mm = _by_id(state)["sequence-2"]["mermaid"]
    assert "n_Checkout-)n_Events: Kafka publish" in mm and "n_Events-)n_Mailer: deliver" in mm
    assert "n_Events-->>" not in mm  # no reply over an asynchronous hop

    # no clients: entry points are components nobody calls; no store reachable: the longest path
    batch = _state([_node("cron", "Scheduler"), _node("etl", "ETL"), _node("api", "Partner API", "external"),
                    _edge("cron", "etl"), _edge("etl", "api", "HTTPS")])
    (flow,) = diagrams.offline_flows(batch)
    assert flow.path == ["cron", "etl", "api"] and flow.name == "Scheduler to Partner API" and not flow.requirement_ids
    # nothing connected: no flows, a placeholder diagram
    lone = _by_id(_state([_node("a", "A")]))
    assert lone["sequence-1"]["empty"] and "No request flows" in lone["sequence-1"]["alt"]


def test_flow_agent_names_and_picks_paths(monkeypatch):
    state = build_template("ai-customer-support")
    calls = []

    def fake(system, messages, schema, **kw):
        calls.append(messages[-1]["content"])
        return {"flows": [
            {"name": "Customer asks where their order is", "candidate": 0, "requirement_ids": ["REQ-003", "REQ-999"]},
            {"name": "Web customer looks up an order", "path": ["web-app", "API Gateway", "support-agent", "orders-mcp",
                                                                "order-service", "orders-db"]},
            {"name": "Invented path", "path": ["web-app", "orders-db"]},  # not connected, no candidate: dropped
            {"name": "", "candidate": 0},
        ]}

    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", fake)
    found, engine = diagrams.flows(state, ai=True)
    assert engine == "llm" and [f.name for f in found] == ["Customer asks where their order is",
                                                           "Web customer looks up an order"]
    assert found[0].requirement_ids == ["REQ-003"] and found[1].path[0] == "web-app"
    assert '"candidates"' in calls[0] and "Orders DB" in calls[0]
    # cached per model state: listing and opening a diagram agree without a second call
    d = _by_id(state, ai=True)
    assert d["sequence-2"]["flow"] == "Web customer looks up an order" and d["sequence-2"]["engine"] == "llm"
    assert len(calls) == 1
    # without ai=true the agent is not asked
    assert diagrams.flows(state)[1] == "offline"

    diagrams._cache.clear()
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"flows": [{"name": "x", "candidate": 7}]})
    found, engine = diagrams.flows(state, ai=True)
    assert engine == "offline" and found[0].name == "Order lookup"

    def down(*a, **k):
        raise llm.LLMUnavailable("down")
    diagrams._cache.clear()
    monkeypatch.setattr(llm, "complete_json", down)
    assert diagrams.flows(state, ai=True)[1] == "offline"


# --------------------------------------------------------------------------- ER
def test_er_across_all_components():
    state = _with_lld(build_template("ai-customer-support"), "orders-db", "order-service")
    model = diagrams.er_model(state)
    names = {t["name"] for t in model["tables"]}
    assert {"orders", "order_items", "audit_log"} <= names
    assert {t["database"] for t in model["tables"]} == {"Orders DB"}
    by_name = {t["id"]: t["name"] for t in model["tables"]}
    rels = {(by_name[r["from"]], by_name[r["to"]], r["label"]) for r in model["relations"]}
    assert ("orders", "order_items", "1:N") in rels  # from the LLD connection

    er = _by_id(state)["er"]
    mm = er["mermaid"]
    assert mm.startswith("erDiagram") and 't_orders["orders"] {' in mm and "uuid id PK" in mm
    assert 't_orders ||--o{ t_order_items : "1:N"' in mm
    pu = er["plantuml"]
    assert 'package "Orders DB" {' in pu and 'entity "orders" as t_orders {' in pu
    assert "* id : uuid <<PK>>" in pu and "t_orders ||--o{ t_order_items : 1:N" in pu
    assert er["tables"] == len(model["tables"]) and "in 1 database (Orders DB:" in er["alt"]


def test_er_infers_foreign_keys_and_groups_by_database():
    state = _state([
        _node("db1", "Shop DB", "database"), _node("svc", "Billing"),
        _node("customers", "customers", "table", parent_id="db1", props={"columns": "id uuid pk, email text uk"}),
        _node("orders", "orders", "table", parent_id="db1", props={"columns": "id uuid pk, customer_id uuid fk"}),
        _node("inv", "invoices", "table", parent_id="svc",
              props={"columns": "id uuid pk, order_id uuid fk, total numeric(10)"}),
    ])
    model = diagrams.er_model(state)
    rels = {(r["from"], r["to"], r["label"]) for r in model["relations"]}
    assert rels == {("customers", "orders", "customer_id"), ("orders", "inv", "order_id")}
    dbs = {t["name"]: t["database"] for t in model["tables"]}
    assert dbs == {"customers": "Shop DB", "orders": "Shop DB", "invoices": "Billing (owned tables)"}
    mm = _by_id(state)["er"]["mermaid"]
    assert "numeric_10 total" in mm and "text email UK" in mm


# --------------------------------------------------------------------------- deployment
def test_deployment_groups_by_platform_and_runtime():
    state = _state([
        _node("web", "Web", "client", "React"),
        _node("fn", "Thumbnailer", "service", "AWS Lambda"),
        _node("api", "Orders API", "service", "Go", vendor="aws", props={"runtime": "EKS"}),
        _node("rds", "Orders DB", "database", "Amazon Aurora", vendor="aws"),
        _node("run", "Search", "service", "Cloud Run"),
        _node("pay", "Stripe", "external", "Stripe"),
        _node("legacy", "Legacy ERP", "service", "Java"),
    ])
    assert diagrams.placement(state.nodes["fn"]) == ("aws", "Serverless")
    assert diagrams.placement(state.nodes["api"]) == ("aws", "Kubernetes")
    assert diagrams.placement(state.nodes["rds"]) == ("aws", "Managed data services")
    assert diagrams.placement(state.nodes["run"]) == ("gcp", "Serverless")
    assert diagrams.placement(state.nodes["pay"])[0] == "saas"
    assert diagrams.placement(state.nodes["legacy"]) == ("self", "Application runtime")
    assert diagrams.placement(state.nodes["web"]) == ("devices", "Browsers and apps")
    dep = _by_id(state)["deployment"]
    assert 'subgraph g_aws["AWS"]' in dep["mermaid"] and 'subgraph g_aws_Kubernetes["Kubernetes"]' in dep["mermaid"]
    assert 'cloud "AWS" as g_aws {' in dep["plantuml"] and 'node "Kubernetes" as g_aws_Kubernetes {' in dep["plantuml"]
    assert "AWS (Serverless: Thumbnailer; Kubernetes: Orders API; Managed data services: Orders DB)" in dep["alt"]


# --------------------------------------------------------------------------- data flow
def test_dataflow_trust_boundaries():
    d = _by_id(build_template("ai-customer-support"))["dataflow"]
    assert 'subgraph z_public["Public zone (untrusted clients)"]' in d["mermaid"]
    assert 'n_Web_App ==>|"HTTPS"| n_API_Gateway' in d["mermaid"]  # crosses a boundary: thick
    assert 'n_Orders_MCP_Server -->|"REST"| n_Order_Service' in d["mermaid"]  # same zone
    assert d["crossings"] == 6 and d["unguarded"] == []
    assert "Security controls: Auth Service" in d["alt"] and "Every public flow enters through the edge" in d["alt"]
    assert "n_WhatsApp -[bold]-> n_API_Gateway : HTTPS webhook" in d["plantuml"]

    risky = _by_id(_state([_node("app", "App", "client"), _node("svc", "Svc"), _node("db", "DB", "database"),
                           _edge("app", "svc", "HTTP"), _edge("app", "db")]))["dataflow"]
    assert risky["unguarded"] == ["App to Svc", "App to DB"]
    assert '|"HTTP (no gateway)"|' in risky["mermaid"] and "Warning: App to Svc and App to DB reach" in risky["alt"]
    assert "No security components" in risky["alt"]


# --------------------------------------------------------------------------- escaping
NASTY = 'Orders "Primary" DB #1; <v2> {x} | 100% `y` C:\\data'


def _nasty_state() -> ProjectState:
    return _state([
        {"op": "add_requirement", "requirement": {"id": "REQ-1", "title": 'Pay "now"; #fast'}},
        _node("end", "end", "client", requirement_ids=["REQ-1"]),
        _node("svc", "Svc -> [graph] (v2)", "service", 'Node.js "LTS"', requirement_ids=["REQ-1"]),
        _node("db", NASTY, "database", "Postgres; 16", requirement_ids=["REQ-1"]),
        _node("tbl", 'line "items"', "table", parent_id="db", props={"columns": 'id uuid pk, na-me "x" fk, ok text'}),
        _node("tbl2", "line items", "table", parent_id="db", props={"columns": "id uuid pk"}),
        {"op": "add_edge", "edge": {"source": "end", "target": "svc", "protocol": 'HTTPS "1.1"', "label": "a|b;c"}},
        {"op": "add_edge", "edge": {"source": "svc", "target": "db", "protocol": "SQL; DROP"}},
        {"op": "add_edge", "edge": {"source": "tbl", "target": "tbl2", "label": 'has "many" {}'}},
    ])


def test_special_characters_are_escaped():
    assert diagrams.mermaid_text('a "b" #1; <c> {d} | e% `f`') == \
        "a #34;b#34; #35;1#59; #60;c#62; #123;d#125; #124; e#37; #96;f#96;"
    assert diagrams.plantuml_text('a "b"\nc\\d') == "a 'b' c/d"
    found = diagrams.all_diagrams(_nasty_state(), 'My "App"; %% v2')
    for d in found:
        assert not d["empty"], d["id"]
        for line in d["mermaid"].splitlines()[3:]:
            body = line.strip()
            if body.startswith(("accTitle", "accDescr", "classDef", "class ")):
                continue
            # every remaining quote delimits a label: labels themselves never contain raw quotes or separators
            assert body.count('"') % 2 == 0, (d["id"], line)
            for label in body.split('"')[1::2]:
                plain = re.sub(r"#\d+;", "", label.replace("<br/><small>", "").replace("</small>", ""))
                assert not set(plain) & set(';<>{}|`%#'), (d["id"], line)
        assert "%%" not in d["mermaid"], d["id"]
        for line in d["plantuml"].splitlines():
            assert line.count('"') % 2 == 0, (d["id"], line)
    by = {d["id"]: d for d in found}
    # ids are prefixed, so a component named "end" cannot close a subgraph
    assert "n_end([" in by["hld"]["mermaid"] and "\n    end([" not in by["hld"]["mermaid"]
    assert "Orders #34;Primary#34; DB #35;1#59; #60;v2#62; #123;x#125; #124; 100#37; #96;y#96; C:\\data" in by["hld"]["mermaid"]
    assert "Orders 'Primary' DB #1; <v2> {x} | 100% `y` C:/data" in by["hld"]["plantuml"]
    assert "actor n_end as end" in by["sequence-1"]["mermaid"]
    er = by["er"]["mermaid"]
    assert 't_line__items["line #34;items#34;"] {' in er and 't_line_items["line items"]' in er and "x na_me FK" in er
    assert ': "has many"' in er


def _parse_with_mermaid(sources: list[str]) -> list[dict]:
    node = shutil.which("node")
    if not node or not (FRONTEND / "node_modules" / "mermaid").is_dir():
        pytest.skip("Node.js or the frontend's Mermaid (npm ci in frontend/) is not available")
    out = subprocess.run([node, str(Path(__file__).with_name("mermaid_parse.mjs")), str(FRONTEND)],
                         input=json.dumps(sources), capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_mermaid_sources_parse_with_mermaid():
    """Every non-empty diagram (template, with LLD, the special-character project) is valid for Mermaid itself."""
    states = [build_template("ai-customer-support"), _with_lld(build_template("ai-customer-support"), "orders-db"),
              _nasty_state()]
    sources = [(d["id"], d["mermaid"]) for st in states for d in diagrams.all_diagrams(st, 'Shop "x"; #1')
               if d["mermaid"]]
    results = _parse_with_mermaid([s for _, s in sources])
    bad = [(i, r["error"]) for (i, _), r in zip(sources, results) if not r["ok"]]
    assert not bad, bad
    assert {r["type"] for r in results} == {"flowchart-v2", "sequence", "er"}


# --------------------------------------------------------------------------- API and MCP
@pytest.fixture()
def client():
    with new_client(create_app(Store(":memory:")), base_url=BASE) as c:
        signup(c)
        yield c


def test_api_lists_and_serves_diagrams(client):
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    r = client.get(f"/api/projects/{pid}/diagrams")
    assert r.status_code == 200, r.text
    listed = r.json()["diagrams"]
    assert [d["id"] for d in listed] == IDS and all("mermaid" not in d and d["alt"] for d in listed)
    assert next(d for d in listed if d["id"] == "er")["empty"]

    d = client.get(f"/api/projects/{pid}/diagrams/c4-container").json()
    assert d["mermaid"].startswith("flowchart TB") and "System_Boundary" in d["plantuml"] and d["alt"]
    r = client.get(f"/api/projects/{pid}/diagrams/sequence-1", params={"format": "plantuml"})
    assert r.status_code == 200 and r.text.startswith("@startuml") and "text/plain" in r.headers["content-type"]
    assert 'filename="Shop-main-sequence-1.puml"' in r.headers["content-disposition"]
    r = client.get(f"/api/projects/{pid}/diagrams/hld", params={"format": "mermaid"})
    assert r.text.startswith("flowchart") and r.headers["content-disposition"].endswith('.mmd"')
    assert client.get(f"/api/projects/{pid}/diagrams/hld", params={"format": "svg"}).status_code == 422
    assert client.get(f"/api/projects/{pid}/diagrams/nope").status_code == 404
    assert client.get(f"/api/projects/{pid}/diagrams", params={"branch": "nope"}).status_code == 404

    # viewers can read; a stranger cannot
    viewer = member_client(client.app, client, "viewer", "viewer@example.com")
    assert viewer.get(f"/api/projects/{pid}/diagrams/er").status_code == 200
    stranger = new_client(client.app)
    signup(stranger, "stranger@example.com", "Stranger")
    assert stranger.get(f"/api/projects/{pid}/diagrams").status_code in (403, 404)


def test_mcp_get_diagram(client):
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    _, tokens = oauth_login(client)
    at = tokens["access_token"]
    err, out, text = tool(client, at, "get_diagram", project_id=pid)
    assert not err, text
    assert [d["id"] for d in out["diagrams"]] == IDS
    err, out, _ = tool(client, at, "get_diagram", project_id=pid, diagram="dataflow", format="plantuml")
    assert not err and out["source"].startswith("@startuml") and out["format"] == "plantuml" and out["alt"]
    err, _, text = tool(client, at, "get_diagram", project_id=pid, diagram="nope")
    assert err and "available: hld" in text
    err, _, text = tool(client, at, "get_diagram", project_id=pid, diagram="hld", format="png")
    assert err and "format" in text
