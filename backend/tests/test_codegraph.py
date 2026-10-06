"""Repository analysis: detections, normalized graphs, reconstruction, drift, impact, sources, the API and MCP.

Most tests use a synthetic normalized graph and need no Graphify. Tests marked ``needs_graphify`` run the real
adapter (in-process and in the worker subprocess) on a tiny fixture repository and are skipped when the optional
``codegraph`` extra is not installed. No test touches the network: URL checks use literal IPs or a fake resolver.
"""

import json
import os
import time

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import member_client, new_client, signup  # noqa: E402

from archflow import codegraph  # noqa: E402
from archflow.codegraph import detect, drift, impact, normalize, reconstruct, sources  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402

from test_mcp_remote import BASE, oauth_login, tool  # noqa: E402

needs_graphify = pytest.mark.skipif(not codegraph.available(), reason="codegraph extra (graphifyy) not installed")


# --------------------------------------------------------------------------- a synthetic normalized graph
def _sym(sid, name, file, kind="function", community=0, line=1):
    return {"id": sid, "name": name, "file": file, "line": line, "kind": kind, "community": community}


def synthetic_graph() -> dict:
    files = ["services/orders/app.py", "services/orders/db.py", "services/orders/cache.py",
             "services/orders/tests/test_app.py", "web/src/main.tsx", "web/src/api.ts"]
    symbols = [_sym(f"f{i}", f.split("/")[-1], f, "file", community=0 if "orders" in f else 1)
               for i, f in enumerate(files)]
    symbols += [
        _sym("app_create", "create_order", "services/orders/app.py", line=10),
        _sym("app_list", "list_orders", "services/orders/app.py", line=30),
        _sym("db_save", "save_order", "services/orders/db.py", line=5),
        _sym("db_class", "OrderRepo", "services/orders/db.py", "class", line=3),
        _sym("cache_get", "cached", "services/orders/cache.py", line=4),
        _sym("test_fn", "test_create", "services/orders/tests/test_app.py", line=2),
        _sym("web_main", "render", "web/src/main.tsx", community=1),
        _sym("web_load", "loadOrders", "web/src/api.ts", community=1),
    ]
    edges = [
        {"source": "f0", "target": "app_create", "relation": "contains", "confidence": "EXTRACTED"},
        {"source": "f1", "target": "db_save", "relation": "contains", "confidence": "EXTRACTED"},
        {"source": "db_class", "target": "db_save", "relation": "method", "confidence": "EXTRACTED"},
        {"source": "app_create", "target": "db_save", "relation": "calls", "confidence": "EXTRACTED"},
        {"source": "app_list", "target": "cache_get", "relation": "calls", "confidence": "INFERRED"},
        {"source": "f0", "target": "f1", "relation": "imports_from", "confidence": "EXTRACTED"},
        {"source": "test_fn", "target": "app_create", "relation": "calls", "confidence": "EXTRACTED"},
        {"source": "web_main", "target": "web_load", "relation": "calls", "confidence": "EXTRACTED"},
        {"source": "web_load", "target": "ghost", "relation": "calls", "confidence": "EXTRACTED"},  # dropped
    ]
    return {
        "format": codegraph.FORMAT, "generator": {"tool": "test", "version": "1"}, "root_name": "shop",
        "files": [{"path": f, "language": detect.language_of(f), "symbols": 1} for f in files],
        "symbols": symbols, "edges": edges,
        "communities": [{"id": 0, "size": 9, "label": "create_order", "paths": ["services/orders"]},
                        {"id": 1, "size": 4, "label": "render", "paths": ["web/src"]}],
        "detections": {
            "languages": {"Python": 4, "TypeScript": 2},
            "manifests": [
                {"path": "services/orders/pyproject.toml", "dir": "services/orders", "kind": "python", "name": "orders",
                 "dependencies": ["fastapi", "psycopg", "redis"], "scripts": {}},
                {"path": "web/package.json", "dir": "web", "kind": "node", "name": "shop-web",
                 "dependencies": ["react", "react-dom"], "scripts": {"dev": "vite"}},
            ],
            "compose_services": [], "dockerfiles": [], "k8s": [], "terraform": [],
            "infrastructure": [
                {"key": "postgresql", "label": "PostgreSQL", "category": "database", "type": "database",
                 "catalog_id": "oss-postgresql", "technology": "PostgreSQL", "files": ["services/orders/db.py"],
                 "evidence": []},
                {"key": "redis", "label": "Redis", "category": "cache", "type": "cache", "catalog_id": "oss-redis",
                 "technology": "Redis", "files": ["services/orders/cache.py"], "evidence": []},
            ],
            "frameworks": [{"name": "FastAPI", "kind": "web-framework", "files": ["services/orders/app.py"]},
                           {"name": "React", "kind": "ui", "files": ["web/src/main.tsx"]}],
            "entry_points": [{"path": "services/orders/app.py", "kind": "web-app", "name": "FastAPI"},
                             {"path": "web/src/main.tsx", "kind": "web-client", "name": "main.tsx"}],
        },
        "analysis": {"god_nodes": [{"id": "db_save", "name": "save_order", "degree": 3}], "import_cycles": []},
    }


def designed_state() -> ProjectState:
    state, _ = apply_ops(ProjectState(), [
        {"op": "add_requirement", "requirement": {"id": "REQ-001", "title": "Customers place orders"}},
        {"op": "add_requirement", "requirement": {"id": "REQ-002", "title": "Order data is durable"}},
        {"op": "add_node", "node": {"id": "web", "name": "Web", "type": "client", "technology": "React"}},
        {"op": "add_node", "node": {"id": "order-svc", "name": "Order Service", "technology": "FastAPI",
                                    "requirement_ids": ["REQ-001"]}},
        {"op": "add_node", "node": {"id": "orders-db", "name": "Orders DB", "type": "database",
                                    "technology": "MySQL", "requirement_ids": ["REQ-002"]}},
        {"op": "add_node", "node": {"id": "pay-svc", "name": "Payments Service", "technology": "Go"}},
        {"op": "add_edge", "edge": {"source": "web", "target": "order-svc"}},
        {"op": "add_edge", "edge": {"source": "pay-svc", "target": "orders-db"}},
    ])
    return state


# --------------------------------------------------------------------------- normalize
def test_validate_cleans_and_rejects():
    g = normalize.validate(synthetic_graph())
    assert g["stats"] == {"files": 6, "symbols": 14, "edges": 8, "communities": 2}  # dangling edge dropped
    bad = synthetic_graph()
    bad["format"] = "graphify/raw"
    with pytest.raises(ValueError, match="unsupported format"):
        normalize.validate(bad)
    with pytest.raises(ValueError):
        normalize.validate([1, 2])
    trav = synthetic_graph()
    trav["files"][0]["path"] = "../../etc/passwd"
    assert normalize.validate(trav)["files"][0]["path"] == "etc/passwd"
    assert normalize.decompress(normalize.compress(g)) == g
    s = normalize.summarize(g)
    assert [i["label"] for i in s["infrastructure"]] == ["PostgreSQL", "Redis"] and s["stats"]["symbols"] == 14


# --------------------------------------------------------------------------- reconstruction
def test_reconstruct_services_infra_and_connections():
    g = normalize.validate(synthetic_graph())
    r = reconstruct.reconstruct(g, repo_url="https://github.com/acme/shop", commit_sha="abc1234def")
    by_name = {n["name"]: n for n in r["nodes"]}
    assert set(by_name) == {"Orders", "Web", "PostgreSQL", "Redis"}
    orders, web = by_name["Orders"], by_name["Web"]
    assert orders["type"] == "service" and orders["technology"].startswith("FastAPI")
    assert orders["props"]["code_paths"] == "services/orders" and orders["props"]["symbols"] == "5"  # tests excluded
    assert orders["props"]["code_repo"] == "https://github.com/acme/shop" and orders["props"]["code_commit"] == "abc1234def"
    assert web["type"] == "client" and web["catalog_id"] == "oss-react"
    assert by_name["PostgreSQL"]["catalog_id"] == "oss-postgresql" and by_name["Redis"]["type"] == "cache"
    pairs = {(e["source"], e["target"]): e for e in r["edges"]}
    assert (orders["id"], by_name["PostgreSQL"]["id"]) in pairs and pairs[(orders["id"], by_name["PostgreSQL"]["id"])]["protocol"] == "SQL"
    assert (orders["id"], by_name["Redis"]["id"]) in pairs
    assert pairs[(web["id"], orders["id"])]["label"] == "inferred"
    state = reconstruct.to_state(r)
    assert len(state.nodes) == 4 and len(state.edges) == 3


def test_reconstruct_compose_names_and_images():
    g = synthetic_graph()
    g["detections"]["compose_services"] = [
        {"name": "orders-api", "file": "docker-compose.yml", "image": "", "build": "services/orders",
         "depends_on": ["db", "mq"], "ports": [], "infra": None},
        {"name": "db", "file": "docker-compose.yml", "image": "postgres:16", "build": None, "depends_on": [],
         "ports": [], "infra": "postgresql"},
        {"name": "mq", "file": "docker-compose.yml", "image": "rabbitmq:3", "build": None, "depends_on": [],
         "ports": [], "infra": "rabbitmq"},
        {"name": "proxy", "file": "docker-compose.yml", "image": "nginx:1.27", "build": None, "depends_on": ["orders-api"],
         "ports": ["80:80"], "infra": None},
    ]
    r = reconstruct.reconstruct(normalize.validate(g))
    by_name = {n["name"]: n for n in r["nodes"]}
    assert "Orders Api" in by_name and "RabbitMQ" in by_name and by_name["Proxy"]["type"] == "gateway"
    ids = {n["name"]: n["id"] for n in r["nodes"]}
    pairs = {(e["source"], e["target"]) for e in r["edges"]}
    assert (ids["Orders Api"], ids["RabbitMQ"]) in pairs and (ids["Proxy"], ids["Orders Api"]) in pairs


def test_refine_is_silent_offline():
    g = normalize.validate(synthetic_graph())
    r = reconstruct.reconstruct(g)
    before = json.dumps(r["nodes"], sort_keys=True, default=str)
    assert reconstruct.refine(r, g) == "offline"
    assert json.dumps(r["nodes"], sort_keys=True, default=str) == before


# --------------------------------------------------------------------------- drift
def test_drift_detects_all_kinds():
    code = reconstruct.to_state(reconstruct.reconstruct(normalize.validate(synthetic_graph())))
    d = drift.drift(designed_state(), code)
    by_code = {}
    for i in d["issues"]:
        by_code.setdefault(i["code"], []).append(i)
    matched = {m["node_id"]: m["code_name"] for m in d["matches"]}
    assert matched == {"web": "Web", "order-svc": "Orders", "orders-db": "PostgreSQL"}
    redis = by_code["drift_code_only"]
    assert len(redis) == 1 and "Order Service talks to Redis" in redis[0]["message"] and redis[0]["node_ids"] == ["order-svc"]
    assert [i["node_ids"] for i in by_code["drift_design_only"]] == [["pay-svc"]]
    assert by_code["drift_design_only"][0]["severity"] == "info"
    conn = by_code["drift_connection"]
    assert len(conn) == 1 and conn[0]["node_ids"] == ["order-svc", "orders-db"]
    tech = by_code["drift_technology"]
    assert len(tech) == 1 and "MySQL" in tech[0]["message"] and "PostgreSQL" in tech[0]["message"]
    assert 0 <= d["alignment"] < 100


def test_drift_clean_when_design_matches_code():
    recon = reconstruct.reconstruct(normalize.validate(synthetic_graph()))
    code = reconstruct.to_state(recon)
    d = drift.drift(code.model_copy(deep=True), code)
    assert d["issues"] == [] and d["alignment"] == 100


def test_code_paths_give_explicit_traceability():
    code = reconstruct.to_state(reconstruct.reconstruct(normalize.validate(synthetic_graph())))
    designed, _ = apply_ops(ProjectState(), [{"op": "add_node", "node": {
        "id": "x", "name": "Fulfilment", "props": {"code_paths": "services/orders"}}}])
    assert drift.match(designed, code)["x"][0] == "orders"


# --------------------------------------------------------------------------- impact
def test_impact_files_to_requirements():
    g = normalize.validate(synthetic_graph())
    recon = reconstruct.reconstruct(g)
    code = reconstruct.to_state(recon)
    out = impact.impact(g, recon, code, designed_state(), ["./services/orders/db.py", "nope/missing.py"])
    assert out["changed_files"] == ["services/orders/db.py"] and out["unmatched_files"] == ["nope/missing.py"]
    names = {s["name"] for s in out["affected_symbols"]}
    assert {"create_order", "test_create"} <= names  # callers, two hops
    assert {c["name"] for c in out["code_components"]} == {"Orders", "PostgreSQL"}
    assert {c["id"] for c in out["components"]} == {"order-svc", "orders-db"}
    assert [r["id"] for r in out["requirements"]] == ["REQ-001", "REQ-002"]
    shallow = impact.affected_symbols(g, ["services/orders/db.py"], depth=1)
    assert {s["name"] for s in shallow["affected_symbols"]} == {"create_order", "app.py"}  # caller + importer


# --------------------------------------------------------------------------- detections (no Graphify needed)
def make_fixture_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "api").mkdir()
    (root / "web" / "src").mkdir(parents=True)
    (root / "api" / "__init__.py").write_text("")
    (root / "api" / "main.py").write_text(
        "import redis\nfrom fastapi import FastAPI\nfrom api.store import save\n\napp = FastAPI()\n"
        "cache = redis.Redis()\n\n\n@app.post('/orders')\ndef create_order():\n    return save({'id': 1})\n")
    (root / "api" / "store.py").write_text(
        "import psycopg\n\n\ndef save(order):\n    return connect(order)\n\n\ndef connect(order):\n    return order\n")
    (root / "api" / "requirements.txt").write_text("fastapi>=0.110\nredis==5.0\npsycopg[binary]\n")
    (root / "web" / "package.json").write_text(json.dumps({"name": "web", "dependencies": {"react": "^19"}}))
    (root / "web" / "src" / "api.ts").write_text(
        "import { fmt } from './fmt';\nexport function loadOrders(): string { return fmt('orders'); }\n")
    (root / "web" / "src" / "fmt.ts").write_text("export function fmt(s: string): string { return s.trim(); }\n")
    (root / "web" / "src" / "main.tsx").write_text(
        "import { createRoot } from 'react-dom/client';\nimport { loadOrders } from './api';\n"
        "createRoot(document.body).render(loadOrders());\n")
    (root / "docker-compose.yml").write_text(
        "services:\n  api:\n    build: ./api\n    depends_on: [cache]\n  cache:\n    image: redis:7-alpine\n")
    (root / "node_modules" / "junk").mkdir(parents=True)
    (root / "node_modules" / "junk" / "index.js").write_text("require('kafkajs')\n")
    return root


def test_detect_fixture(tmp_path):
    det = detect.detect(make_fixture_repo(tmp_path))
    assert det["languages"] == {"Python": 3, "TypeScript": 3}  # node_modules skipped
    assert {m["path"] for m in det["manifests"]} == {"api/requirements.txt", "web/package.json"}
    assert [(c["name"], c["build"], c["infra"]) for c in det["compose_services"]] == [("api", "api", None),
                                                                                     ("cache", None, "redis")]
    infra = {i["key"]: i for i in det["infrastructure"]}
    assert set(infra) == {"postgresql", "redis"}
    assert infra["redis"]["files"] == ["api/main.py"] and infra["redis"]["evidence"]
    assert {f["name"] for f in det["frameworks"]} == {"FastAPI", "React"}
    assert {(e["path"], e["kind"]) for e in det["entry_points"]} >= {("api/main.py", "web-app"),
                                                                     ("web/src/main.tsx", "web-client")}


def test_detect_parsers():
    assert detect.imported_modules("a.py", "import os, google.genai\nfrom .x import y\nfrom openai import OpenAI\n") == \
        {"os", "google.genai", "openai"}
    assert detect.libs_for("a.py", {"google.genai", "sqlalchemy.orm"}) == {"llm:Gemini", "postgresql"}
    assert detect.imported_modules("a.ts", "import x from '@anthropic-ai/sdk/foo';\nconst r = require('ioredis');\n"
                                           "import './local';\n") == {"@anthropic-ai/sdk", "ioredis"}
    k8s = detect.parse_k8s("k.yaml", "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: api}\n"
                                     "spec:\n  template:\n    spec:\n      containers:\n      - image: mongo:7\n")
    assert k8s == [{"kind": "Deployment", "name": "api", "file": "k.yaml", "images": ["mongo:7"]}]
    tf = detect.parse_terraform("main.tf", 'resource "aws_s3_bucket" "assets" {\n}\nresource "aws_sqs_queue" "jobs" {}\n')
    assert [detect.tf_infra(r["type"]) for r in tf] == ["s3", "sqs"]
    infra = detect.infrastructure({}, [], k8s, tf)
    assert {i["key"] for i in infra} == {"mongodb", "s3", "sqs"}
    assert detect.parse_dockerfile("svc/Dockerfile", "FROM python:3.12-slim AS base\nEXPOSE 8000\nCMD [\"uvicorn\"]\n") == \
        {"path": "svc/Dockerfile", "dir": "svc", "base_images": ["python:3.12-slim"], "expose": ["8000"],
         "cmd": "[\"uvicorn\"]"}
    assert detect.is_test_path("backend/tests/test_x.py") and detect.is_test_path("src/a.spec.ts")
    assert not detect.is_test_path("src/testing_utils.py")


# --------------------------------------------------------------------------- sources (no network)
def fake_resolver(mapping):
    def resolve(host):
        if host not in mapping:
            raise sources.SourceError(f"cannot resolve host '{host}'")
        return mapping[host]
    return resolve


@pytest.mark.parametrize("url, msg", [
    ("http://github.com/a/b", "https"), ("git@github.com:a/b.git", "https"), ("file:///etc", "https"),
    ("https://localhost/a/b", "not allowed"), ("https://127.0.0.1/a/b", "private"),
    ("https://10.1.2.3/a/b", "private"), ("https://192.168.0.10/a", "private"),
    ("https://169.254.169.254/latest/meta-data", "private"), ("https://[::1]/a", "private"),
    ("https://100.64.0.1/a", "private"), ("https://user:pw@github.com/a/b", "credentials"),
    ("https://github.com:8443/a/b", "port"), ("https://internal.corp/a", "private"),
    ("https://evil.example/a", "cannot resolve"), ("https://github.com/a/../b", "path"),
])
def test_repo_url_rejected(url, msg):
    resolver = fake_resolver({"github.com": ["140.82.112.3"], "internal.corp": ["10.0.0.7", "140.82.112.3"]})
    with pytest.raises(sources.SourceError, match=msg):
        sources.validate_repo_url(url, resolver)


def test_repo_url_accepted_and_web_base():
    resolver = fake_resolver({"github.com": ["140.82.112.3"]})
    assert sources.validate_repo_url("https://github.com/acme/shop.git", resolver) == \
        ("https://github.com/acme/shop.git", "github.com", "140.82.112.3")
    assert sources.repo_web_base("https://github.com/acme/shop.git") == "https://github.com/acme/shop"
    assert sources.repo_web_base("https://gitlab.com/g/sub/proj") == "https://gitlab.com/g/sub/proj"
    assert sources.repo_web_base("https://example.com/x") is None


def test_local_path_needs_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("ARCHFLOW_ALLOW_LOCAL_REPOS", raising=False)
    with pytest.raises(sources.SourceError, match="disabled"):
        sources.check_local_path(str(tmp_path))
    monkeypatch.setenv("ARCHFLOW_ALLOW_LOCAL_REPOS", "1")
    assert sources.check_local_path(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(sources.SourceError, match="absolute"):
        sources.check_local_path("relative/dir")


# --------------------------------------------------------------------------- adapter (needs Graphify)
@needs_graphify
def test_adapter_on_fixture_repo(tmp_path):
    from archflow.codegraph.adapter import analyze_directory

    repo = make_fixture_repo(tmp_path / "repo")
    g = analyze_directory(repo, tmp_path / "cache")
    assert g["format"] == codegraph.FORMAT and g["generator"]["tool"] == "graphify"
    assert g["generator"]["version"] == codegraph.GRAPHIFY_PIN
    names = {s["name"] for s in g["symbols"]}
    assert {"create_order", "save", "connect", "loadOrders", "fmt"} <= names
    by_name = {s["name"]: s["id"] for s in g["symbols"]}
    calls = {(e["source"], e["target"]) for e in g["edges"] if e["relation"] == "calls"}
    assert (by_name["create_order"], by_name["save"]) in calls
    assert all(e["confidence"] in ("EXTRACTED", "INFERRED", "AMBIGUOUS") for e in g["edges"])
    assert g["communities"] and all(s["community"] is not None for s in g["symbols"] if s["kind"] != "file")
    assert {i["key"] for i in g["detections"]["infrastructure"]} == {"postgresql", "redis"}
    assert not (repo / "graphify-out").exists()  # the cache never lands in the analysed tree
    r = reconstruct.reconstruct(g)
    assert {"Api", "Redis", "PostgreSQL"} <= {n["name"] for n in r["nodes"]}


@needs_graphify
def test_worker_subprocess(tmp_path):
    from archflow.codegraph.jobs import run_worker

    repo = make_fixture_repo(tmp_path / "repo")
    work = tmp_path / "work"
    work.mkdir()
    g = run_worker(repo, work)
    assert g["stats"]["symbols"] > 5 and (work / "graph.json").exists()
    with pytest.raises(RuntimeError):
        run_worker(tmp_path / "missing", work)


# --------------------------------------------------------------------------- API
@pytest.fixture()
def env():
    store = Store(":memory:")
    with new_client(create_app(store), base_url=BASE) as client:
        signup(client)
        pid = client.post("/api/projects", json={"name": "Shop"}).json()["id"]
        yield client, store, pid


def upload(client, pid, graph=None, **params):
    return client.post(f"/api/projects/{pid}/codegraph/upload", params=params,
                       content=json.dumps(graph or synthetic_graph()), headers={"Content-Type": "application/json"})


def seed_main(client, pid):
    ops = [{"op": "add_requirement", "requirement": {"id": "REQ-001", "title": "Customers place orders"}},
           {"op": "add_node", "node": {"id": "order-svc", "name": "Order Service", "technology": "FastAPI",
                                       "requirement_ids": ["REQ-001"]}},
           {"op": "add_node", "node": {"id": "orders-db", "name": "Orders DB", "type": "database",
                                       "technology": "MySQL"}}]
    assert client.post(f"/api/projects/{pid}/ops", json={"ops": ops}).status_code == 200


def test_api_upload_list_get(env, monkeypatch):
    client, _, pid = env
    empty = client.get(f"/api/projects/{pid}/codegraph").json()
    assert empty["latest"] is None and empty["available"] == codegraph.available() and "archflow-codegraph" in empty["cli"]
    assert client.get(f"/api/projects/{pid}/codegraph/drift").status_code == 404
    r = upload(client, pid, repo_url="https://github.com/acme/shop", commit_sha="abc1234")
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["source"] == "upload" and rec["summary"]["stats"]["files"] == 6 and "graph" not in rec
    over = client.get(f"/api/projects/{pid}/codegraph").json()
    assert over["latest"]["id"] == rec["id"] and len(over["graphs"]) == 1
    full = client.get(f"/api/projects/{pid}/codegraph/{rec['id']}", params={"full": "true"}).json()
    assert full["graph"]["stats"]["symbols"] == 14
    assert client.get(f"/api/projects/{pid}/codegraph/cg-nope").status_code == 404
    assert upload(client, pid, graph={"format": "x"}).status_code == 422
    assert client.post(f"/api/projects/{pid}/codegraph/upload", content=b"not json").status_code == 422
    assert upload(client, pid, repo_url="http://evil").status_code == 422
    monkeypatch.setattr(normalize, "max_upload_bytes", lambda: 100)
    assert upload(client, pid).status_code == 413


def test_api_reconstruct_drift_analysis_impact(env):
    client, store, pid = env
    seed_main(client, pid)
    gid = upload(client, pid, repo_url="https://github.com/acme/shop", commit_sha="abc1234ff").json()["id"]
    r = client.post(f"/api/projects/{pid}/codegraph/{gid}/reconstruct", json={})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["branch"] == "draft/from-code-abc1234" and out["components"] == 4 and out["engine"] == "offline"
    state = client.get(f"/api/projects/{pid}", params={"branch": out["branch"]}).json()["state"]
    names = {n["name"]: n for n in state["nodes"].values()}
    assert set(names) == {"Orders", "Web", "PostgreSQL", "Redis"}  # designed main nodes replaced
    assert names["Orders"]["props"]["code_paths"] == "services/orders"
    assert names["Orders"]["requirement_ids"] == ["REQ-001"]  # inherited from the matched designed component
    assert "REQ-001" in state["requirements"]
    versions = client.get(f"/api/projects/{pid}/versions").json()
    assert versions[0]["number"] == out["version"] and versions[0]["branch"] == out["branch"]
    assert versions[0]["message"] == "Reconstructed from https://github.com/acme/shop@abc1234ff"
    assert len(store.get_state(pid, "main").nodes) == 2  # main untouched
    again = client.post(f"/api/projects/{pid}/codegraph/{gid}/reconstruct", json={"refine": False}).json()
    assert again["branch"] == "draft/from-code-abc1234-2"
    assert client.post(f"/api/projects/{pid}/codegraph/{gid}/reconstruct",
                       json={"branch_name": "draft/from-code-abc1234"}).status_code == 409

    d = client.get(f"/api/projects/{pid}/codegraph/drift").json()
    codes = {i["code"] for i in d["issues"]}
    assert {"drift_code_only", "drift_technology", "drift_connection"} <= codes and d["code_graph_id"] == gid
    clean = client.get(f"/api/projects/{pid}/codegraph/drift", params={"branch": out["branch"]}).json()
    assert clean["issues"] == [] and clean["alignment"] == 100
    assert client.get(f"/api/projects/{pid}/codegraph/drift", params={"branch": "nope"}).status_code == 404

    a = client.get(f"/api/projects/{pid}/analysis").json()
    assert a["drift_issues"] and 0 <= a["health"]["code_alignment"] < 100

    imp = client.post(f"/api/projects/{pid}/codegraph/impact", json={"files": ["services/orders/db.py"]}).json()
    assert {c["id"] for c in imp["components"]} == {"order-svc", "orders-db"}
    assert [r["id"] for r in imp["requirements"]] == ["REQ-001"]
    imp2 = client.post(f"/api/projects/{pid}/codegraph/impact",
                       json={"files": ["services/orders/db.py"], "branch": out["branch"]}).json()
    assert {c["name"] for c in imp2["components"]} == {"Orders", "PostgreSQL"}
    assert client.post(f"/api/projects/{pid}/codegraph/impact", json={"files": []}).status_code == 422


def test_api_analyze_validation_and_roles(env, monkeypatch):
    client, _, pid = env
    monkeypatch.delenv("ARCHFLOW_ALLOW_LOCAL_REPOS", raising=False)
    bad = ["http://github.com/a/b", "https://localhost/a", "https://127.0.0.1/a", "https://10.0.0.1/a",
           "https://169.254.169.254/x"]
    for url in bad:
        r = client.post(f"/api/projects/{pid}/codegraph/analyze", json={"repo_url": url})
        assert r.status_code == (422 if codegraph.available() else 503), (url, r.text)
    r = client.post(f"/api/projects/{pid}/codegraph/analyze", json={"local_path": "/tmp"})
    assert r.status_code == (422 if codegraph.available() else 503)
    if codegraph.available():
        assert "ARCHFLOW_ALLOW_LOCAL_REPOS" in r.json()["detail"]
        assert client.post(f"/api/projects/{pid}/codegraph/analyze", json={}).status_code == 422
    else:
        assert "uv sync --extra codegraph" in r.json()["detail"]
    assert client.get(f"/api/projects/{pid}/codegraph/jobs/job-nope").status_code == 404

    gid = upload(client, pid).json()["id"]
    viewer = member_client(client.app, client, "viewer", "viewer@example.com")
    assert viewer.post(f"/api/projects/{pid}/codegraph/analyze",
                       json={"repo_url": "https://github.com/a/b"}).status_code == 403
    assert upload(viewer, pid).status_code == 403
    assert viewer.post(f"/api/projects/{pid}/codegraph/{gid}/reconstruct", json={}).status_code == 403
    assert viewer.get(f"/api/projects/{pid}/codegraph").status_code == 200
    assert viewer.get(f"/api/projects/{pid}/codegraph/drift").status_code == 200
    assert viewer.post(f"/api/projects/{pid}/codegraph/impact", json={"files": ["web/src/api.ts"]}).status_code == 200
    stranger = new_client(client.app, base_url=BASE)
    signup(stranger, "stranger@example.com", "Stranger")
    assert stranger.get(f"/api/projects/{pid}/codegraph").status_code == 404


@needs_graphify
def test_api_analyze_local_path_job(env, tmp_path, monkeypatch):
    client, _, pid = env
    monkeypatch.setenv("ARCHFLOW_ALLOW_LOCAL_REPOS", "1")
    repo = make_fixture_repo(tmp_path / "repo")
    r = client.post(f"/api/projects/{pid}/codegraph/analyze", json={"local_path": str(repo)})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] in ("queued", "running") and job["source"] == "local" and "local_path" not in job
    deadline = time.time() + 180
    while job["status"] not in ("done", "failed") and time.time() < deadline:
        time.sleep(0.5)
        job = client.get(f"/api/projects/{pid}/codegraph/jobs/{job['id']}").json()
    assert job["status"] == "done", job
    latest = client.get(f"/api/projects/{pid}/codegraph").json()["latest"]
    assert latest["id"] == job["code_graph_id"] and latest["source"] == "local"
    assert {i["key"] for i in latest["summary"]["infrastructure"]} == {"postgresql", "redis"}
    other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
    assert client.get(f"/api/projects/{other}/codegraph/jobs/{job['id']}").status_code == 404


# --------------------------------------------------------------------------- MCP tools
def test_mcp_codegraph_tools(env):
    client, _, pid = env
    seed_main(client, pid)
    _, tokens = oauth_login(client)
    at = tokens["access_token"]
    err, out, _ = tool(client, at, "get_code_summary", project_id=pid)
    assert not err and out["latest"] is None and "analyze_repository" in out["hint"]
    err, _, text = tool(client, at, "get_drift", project_id=pid)
    assert err and "no code analysis" in text
    upload(client, pid, commit_sha="abc1234")
    err, out, _ = tool(client, at, "get_code_summary", project_id=pid)
    assert not err and out["latest"]["summary"]["stats"]["symbols"] == 14
    err, out, _ = tool(client, at, "get_drift", project_id=pid)
    assert not err and any(i["code"] == "drift_technology" for i in out["issues"])
    err, out, _ = tool(client, at, "get_change_impact", project_id=pid, files=["services/orders/app.py"])
    assert not err and [r["id"] for r in out["requirements"]] == ["REQ-001"]
    err, _, text = tool(client, at, "analyze_repository", project_id=pid, repo_url="https://127.0.0.1/x")
    assert err and ("private" in text if codegraph.available() else "codegraph" in text)
    err, _, text = tool(client, at, "get_code_summary", project_id="p-nope")
    assert err and "not found" in text
    _, ro = oauth_login(client, access="read")
    err, _, text = tool(client, ro["access_token"], "analyze_repository", project_id=pid,
                        repo_url="https://github.com/a/b")
    assert err and "read-only" in text
    err, out, _ = tool(client, ro["access_token"], "get_drift", project_id=pid)
    assert not err
