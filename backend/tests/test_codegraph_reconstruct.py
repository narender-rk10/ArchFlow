"""Architecture reconstruction on a realistic repository: compose merging across files, image typing, container ↔
code merging, dev-tool exclusion, build-time packages, library folding, connections and name validation.

The fixture mimics https://github.com/fastapi/full-stack-fastapi-template (FastAPI backend, React front-end with an
OpenAPI-generated client, react-email templates, copier hooks, Traefik + PostgreSQL + Adminer + Mailpit in two
compose files). Its expected architecture is exactly::

    Web App ──HTTP──▶ Traefik ──HTTP──▶ Backend API ──SQL──▶ PostgreSQL
                         └────HTTP──▶ Web App            (the proxy also serves the front-end)

Gateway model: a gateway routes to every compose service carrying routing labels/config (gateway → service), and a
client reaches a backend through the gateway that routes to it (client → gateway), never directly.

Most tests build the normalized graph from the deterministic detections plus synthetic symbols, so they need no
Graphify; ``test_template_with_graphify`` runs the real adapter and is skipped without the ``codegraph`` extra.
"""

import json
import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402

from archflow import codegraph, llm  # noqa: E402
from archflow.codegraph import detect, impact, normalize, reconstruct  # noqa: E402

needs_graphify = pytest.mark.skipif(not codegraph.available(), reason="codegraph extra (graphifyy) not installed")

COMPOSE = """\
services:
  proxy:
    image: traefik:v3.6
    command:
      - --providers.docker
      - --providers.docker.exposedbydefault=false
  db:
    image: postgres:18
    environment:
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?Variable not set}
      - POSTGRES_DB=app
  adminer:
    image: adminer
    depends_on:
      - db
    labels:
      - traefik.enable=true
      - traefik.http.routers.adminer-http.rule=Host(`adminer.${DOMAIN}`)
  backend:
    image: '${DOCKER_IMAGE_BACKEND?Variable not set}:${TAG-latest}'
    build:
      context: ./backend
    depends_on:
      db:
        condition: service_healthy
    environment:
      SECRET_KEY: ${SECRET_KEY?Variable not set}
      DATABASE_URL: postgresql://postgres:${POSTGRES_PASSWORD:?Variable not set}@db:5432/app
    labels:
      - traefik.enable=true
      - traefik.http.routers.backend-http.rule=Host(`api.${DOMAIN}`)
  frontend:
    image: '${DOCKER_IMAGE_FRONTEND?Variable not set}:${TAG-latest}'
    build:
      context: ./frontend
    labels:
      - traefik.enable=true
      - traefik.http.routers.frontend-http.rule=Host(`dashboard.${DOMAIN}`)
"""
COMPOSE_OVERRIDE = """\
services:
  proxy:
    ports: ["80:80", "8090:8080"]
  db:
    ports: ["5432:5432"]
  adminer:
    ports: ["8080:8080"]
  backend:
    ports: ["8000:8000"]
    build:
      context: ./backend
    environment:
      SMTP_HOST: mailpit
  mailpit:
    image: axllent/mailpit
    ports: ["8025:8025", "1025:1025"]
  frontend:
    build:
      context: ./frontend
      args:
        - VITE_API_URL=http://localhost:8000
"""


def make_template_repo(root):
    """A tree shaped like the full-stack FastAPI template."""
    files = {
        "package.json": json.dumps({"name": "fastapi-full-stack-template", "private": True,
                                    "workspaces": ["frontend", "packages/*"]}),
        "pyproject.toml": '[tool.uv.workspace]\nmembers = ["backend"]\n',
        "hooks/post_gen_project.py": "from pathlib import Path\n\n\ndef fix_line_endings():\n"
                                     "    for path in Path('.').glob('**/*.sh'):\n        path.write_bytes(path.read_bytes())\n\n\n"
                                     "def main():\n    fix_line_endings()\n\n\nmain()\n",
        "scripts/generate_client.py": "import json\n\n\ndef main():\n    print(json.dumps({}))\n",
        # ---- backend
        "backend/pyproject.toml": '[project]\nname = "app"\ndependencies = ["fastapi[standard]", "sqlmodel", '
                                  '"psycopg[binary]", "pydantic-settings", "emails", "jinja2"]\n',
        "backend/Dockerfile": "FROM python:3.12\nWORKDIR /app/\nCOPY ./app /app/app\nCMD [\"fastapi\", \"run\"]\n",
        "backend/app/__init__.py": "",
        "backend/app/main.py": "from fastapi import FastAPI\nfrom app.api.main import api_router\n\n"
                               "app = FastAPI()\napp.include_router(api_router)\n",
        "backend/app/api/__init__.py": "",
        "backend/app/api/main.py": "from fastapi import APIRouter\nfrom app.api.routes import items, users\n\n"
                                   "api_router = APIRouter()\napi_router.include_router(items.router)\n"
                                   "api_router.include_router(users.router)\n",
        "backend/app/api/routes/__init__.py": "",
        "backend/app/api/routes/items.py": "from fastapi import APIRouter\nfrom app import crud\n\nrouter = APIRouter()\n\n\n"
                                           "@router.get('/items/')\ndef read_items():\n    return crud.list_items()\n",
        "backend/app/api/routes/users.py": "from fastapi import APIRouter\nfrom app import crud\nfrom app.utils import "
                                           "send_email\n\nrouter = APIRouter()\n\n\n@router.post('/users/')\n"
                                           "def create_user():\n    send_email('new_account')\n    return crud.create_user()\n",
        "backend/app/core/__init__.py": "",
        "backend/app/core/db.py": "import psycopg\nfrom sqlmodel import Session, create_engine\n\n"
                                  "engine = create_engine('postgresql+psycopg://')\n\n\ndef get_session():\n"
                                  "    return Session(engine)\n",
        "backend/app/crud.py": "from app.core.db import get_session\nfrom app.models import Item, User\n\n\n"
                               "def list_items():\n    return get_session().query(Item)\n\n\n"
                               "def create_user():\n    return User()\n",
        "backend/app/models.py": "from sqlmodel import SQLModel\n\n\nclass Item(SQLModel):\n    id: int\n\n\n"
                                 "class User(SQLModel):\n    id: int\n",
        "backend/app/utils.py": "from pathlib import Path\n\n\ndef render_template(name):\n"
                                "    return (Path(__file__).parent / 'email-templates' / name).read_text()\n\n\n"
                                "def send_email(name):\n    return render_template(name)\n",
        "backend/tests/test_items.py": "from app.crud import list_items\n\n\ndef test_items():\n    assert list_items()\n",
        # ---- frontend (React + OpenAPI-generated client)
        "frontend/package.json": json.dumps({"name": "frontend", "private": True,
                                             "scripts": {"dev": "vite", "build": "vite build",
                                                         "generate-client": "openapi-ts"},
                                             "dependencies": {"react": "^19", "react-dom": "^19", "axios": "1.7.0",
                                                              "@tanstack/react-query": "^5"},
                                             "devDependencies": {"@hey-api/openapi-ts": "0.57.0", "vite": "^6",
                                                                 "@playwright/test": "^1.49"}}),
        "frontend/Dockerfile": "FROM node:20 AS build\nRUN npm run build\nFROM nginx:1\nCOPY --from=build /app/dist "
                               "/usr/share/nginx/html\n",
        "frontend/openapi-ts.config.ts": "export default { input: './openapi.json', output: './src/client' }\n",
        "frontend/src/main.tsx": "import { createRoot } from 'react-dom/client';\nimport { App } from './App';\n"
                                 "import { OpenAPI } from './client';\n\nOpenAPI.BASE = import.meta.env.VITE_API_URL;\n"
                                 "createRoot(document.getElementById('root')!).render(App());\n",
        "frontend/src/App.tsx": "import { Items } from './routes/items';\nexport function App() { return Items(); }\n",
        "frontend/src/routes/items.tsx": "import { ItemsService } from '../client';\nimport { useQuery } from "
                                         "'@tanstack/react-query';\nexport function Items() {\n"
                                         "  return useQuery({ queryKey: ['items'], queryFn: () => ItemsService.readItems() });\n}\n",
        "frontend/src/hooks/useAuth.ts": "import { UsersService } from '../client';\n"
                                         "export function useAuth() { return UsersService.createUser(); }\n",
        "frontend/src/client/index.ts": "export * from './sdk.gen';\nexport * from './core/OpenAPI';\n",
        "frontend/src/client/sdk.gen.ts": "import { request } from './core/request';\nexport class ItemsService {\n"
                                          "  static readItems() { return request('GET', '/api/v1/items/'); }\n}\n"
                                          "export class UsersService {\n  static createUser() { return request('POST', "
                                          "'/api/v1/users/'); }\n}\n",
        "frontend/src/client/types.gen.ts": "export type Item = { id: number };\nexport type User = { id: number };\n",
        "frontend/src/client/core/OpenAPI.ts": "export const OpenAPI = { BASE: '' };\n",
        "frontend/src/client/core/request.ts": "import axios from 'axios';\nimport { OpenAPI } from './OpenAPI';\n"
                                               "export function request(method: string, url: string) {\n"
                                               "  return axios.request({ method, url: OpenAPI.BASE + url });\n}\n",
        "frontend/tests/login.spec.ts": "import { test } from '@playwright/test';\ntest('login', async () => {});\n",
        # ---- build-time email templates (react-email)
        "packages/email-templates/package.json": json.dumps({"name": "emails", "private": True,
                                                             "scripts": {"dev": "email dev --port 3333",
                                                                         "export": "email export --outDir ../../backend/app/email-templates"},
                                                             "dependencies": {"react-email": "4.0.0",
                                                                              "@react-email/components": "0.0.36",
                                                                              "react": "^19"}}),
        "packages/email-templates/emails/new_account.tsx": "import { Html, Text } from '@react-email/components';\n"
                                                           "export default function NewAccount() {\n"
                                                           "  return Html({ children: Text({ children: 'Welcome' }) });\n}\n",
        "packages/email-templates/emails/reset_password.tsx": "import { Html } from '@react-email/components';\n"
                                                              "export default function ResetPassword() { return Html({}); }\n",
        # ---- compose (two files) and a copier template config
        "docker-compose.yml": COMPOSE,
        "docker-compose.override.yml": COMPOSE_OVERRIDE,
        "copier.yml": "project_name:\n  type: str\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def graph_from_repo(root, extra_edges=()):
    """A normalized graph from the real detections plus one file symbol and one function per code file, and the
    given (source file, target file, relation) edges between those functions — no Graphify needed."""
    det = detect.detect(root)
    files, symbols, edges = [], [], []
    for p in detect.walk(root):
        rel = detect.rel(p, root)
        if detect.language_of(rel) not in detect.CODE_LANGS:
            continue
        files.append({"path": rel, "language": detect.language_of(rel), "symbols": 1})
        symbols.append({"id": "file:" + rel, "name": p.name, "file": rel, "kind": "file", "community": 0})
        symbols.append({"id": "fn:" + rel, "name": p.stem, "file": rel, "kind": "function", "community": 0})
    for a, b, rel in extra_edges:
        edges.append({"source": "fn:" + a, "target": "fn:" + b, "relation": rel, "confidence": "INFERRED"})
    return normalize.validate({"format": codegraph.FORMAT, "generator": {"tool": "test", "version": "1"},
                               "root_name": "full-stack-fastapi-template", "files": files, "symbols": symbols,
                               "edges": edges, "communities": [], "detections": det, "analysis": {}})


# Edges a code-graph tool really produces on such a repository and that must NOT become connections:
NOISE_EDGES = [
    ("backend/app/utils.py", "frontend/src/hooks/useAuth.ts", "calls"),          # cross-language name collision
    ("backend/app/models.py", "frontend/src/client/types.gen.ts", "references"),  # generated types
    ("packages/email-templates/emails/new_account.tsx", "frontend/src/App.tsx", "imports"),  # build-time package
    ("hooks/post_gen_project.py", "backend/app/main.py", "calls"),                 # scaffolding hook
    ("frontend/tests/login.spec.ts", "frontend/src/main.tsx", "calls"),            # tests
    ("backend/tests/test_items.py", "backend/app/crud.py", "calls"),
    ("backend/app/main.py", "backend/app/crud.py", "calls"),                       # internal
]


def summary_of(recon):
    names = {n["id"]: n["name"] for n in recon["nodes"]}
    nodes = {n["name"]: n for n in recon["nodes"]}
    pairs = {(names[e["source"]], names[e["target"]]) for e in recon["edges"]}
    return nodes, pairs


EXPECTED_NODES = {"Traefik": "gateway", "Backend API": "service", "Web App": "client", "PostgreSQL": "database"}
EXPECTED_EDGES = {("Web App", "Traefik"), ("Traefik", "Backend API"), ("Traefik", "Web App"),
                  ("Backend API", "PostgreSQL")}


def assert_template_architecture(recon):
    nodes, pairs = summary_of(recon)
    assert {k: v["type"] for k, v in nodes.items()} == EXPECTED_NODES
    assert len(recon["nodes"]) == 4  # no duplicates
    assert nodes["Backend API"]["technology"] == "FastAPI · Python"
    assert nodes["Web App"]["technology"] == "React · TypeScript" and nodes["Web App"]["catalog_id"] == "oss-react"
    assert nodes["PostgreSQL"]["catalog_id"] == "oss-postgresql" and nodes["Traefik"]["catalog_id"] == "oss-traefik"
    assert nodes["Backend API"]["props"]["compose_service"] == "backend"
    assert nodes["Web App"]["props"]["compose_service"] == "frontend"
    assert nodes["PostgreSQL"]["props"]["compose_service"] == "db"
    assert nodes["PostgreSQL"]["props"]["evidence"] == "code+compose"
    assert nodes["Traefik"]["props"]["compose_service"] == "proxy"
    assert pairs == EXPECTED_EDGES
    by_pair = {(e["source"], e["target"]): e for e in recon["edges"]}
    db_edge = by_pair[(nodes["Backend API"]["id"], nodes["PostgreSQL"]["id"])]
    assert db_edge["protocol"] == "SQL"
    assert "generated OpenAPI client" in by_pair[(nodes["Web App"]["id"], nodes["Traefik"]["id"])]["label"]
    assert [d["name"] for d in recon["dev_tools"]] == ["adminer", "mailpit"]
    excluded = {x["path"]: x for x in recon["excluded"]}
    assert excluded["packages/email-templates"]["reason"] == "email templates (build-time)"
    assert excluded["."]["role"] == "tooling"  # the workspace root: only copier hooks


# --------------------------------------------------------------------------- the template, end to end
def test_template_reconstruction(tmp_path):
    g = graph_from_repo(make_template_repo(tmp_path / "repo"), NOISE_EDGES)
    recon = reconstruct.reconstruct(g, repo_url="https://github.com/acme/template", commit_sha="abc1234")
    assert_template_architecture(recon)
    state = reconstruct.to_state(recon)
    assert len(state.nodes) == 4 and len(state.edges) == 4
    s = normalize.summarize(g)
    assert [d["name"] for d in s["dev_tools"]] == ["adminer", "mailpit"]
    assert {c["name"] for c in s["compose_services"]} == {"proxy", "db", "adminer", "backend", "frontend", "mailpit"}


def test_template_include_dev_tools(tmp_path):
    g = graph_from_repo(make_template_repo(tmp_path / "repo"))
    recon = reconstruct.reconstruct(g, include_dev_tools=True)
    nodes, pairs = summary_of(recon)
    assert set(nodes) == set(EXPECTED_NODES) | {"Adminer", "Mailpit"}
    assert nodes["Adminer"]["props"]["dev_tool"] == "true" and nodes["Mailpit"]["technology"] == "mailpit"
    assert {("Adminer", "PostgreSQL"), ("Traefik", "Adminer"), ("Backend API", "Mailpit")} <= pairs
    assert all(d["included"] for d in recon["dev_tools"])


def test_template_impact_and_drift_use_merged_components(tmp_path):
    g = graph_from_repo(make_template_repo(tmp_path / "repo"), NOISE_EDGES)
    recon = reconstruct.reconstruct(g)
    names = {n["id"]: n["name"] for n in recon["nodes"]}
    hit = impact.code_components(recon, g, ["backend/app/core/db.py"])
    assert {names[h] for h in hit} == {"Backend API", "PostgreSQL"}
    assert impact.code_components(recon, g, ["packages/email-templates/emails/new_account.tsx"]) == []


@needs_graphify
def test_template_with_graphify(tmp_path):
    from archflow.codegraph.adapter import analyze_directory

    g = analyze_directory(make_template_repo(tmp_path / "repo"), tmp_path / "cache")
    assert_template_architecture(reconstruct.reconstruct(g))


# --------------------------------------------------------------------------- compose merging
def test_compose_merged_across_files():
    base = detect.parse_compose("docker-compose.yml", COMPOSE)
    override = detect.parse_compose("docker-compose.override.yml", COMPOSE_OVERRIDE)
    deploy = detect.parse_compose("compose.deploy.yml", "services:\n  db:\n    image: postgres:17\n"
                                                         "  backend:\n    labels: [traefik.http.routers.b-https.tls=true]\n")
    # walk order is alphabetical (deploy, override, base): merging must still apply base → override → others
    merged = {s["name"]: s for s in detect.merge_compose(deploy + override + base)}
    assert list(merged) == ["proxy", "db", "adminer", "backend", "frontend", "mailpit"]
    db = merged["db"]
    assert db["image"] == "postgres:17" and db["ports"] == ["5432:5432"] and db["infra"] == "postgresql"
    assert db["files"] == ["docker-compose.yml", "docker-compose.override.yml", "compose.deploy.yml"]
    be = merged["backend"]
    assert be["build"] == "backend" and be["depends_on"] == ["db"] and be["ports"] == ["8000:8000"]
    assert be["env_hosts"] == ["db", "mailpit"] and detect.traefik_routed(be)
    assert "traefik.http.routers.b-https.tls=true" in be["labels"]
    assert merged["proxy"]["image"] == "traefik:v3.6" and not merged["proxy"]["dev_tool"]
    assert merged["adminer"]["dev_tool"] and merged["mailpit"]["dev_tool"] and not merged["frontend"]["dev_tool"]
    assert merged["mailpit"]["files"] == ["docker-compose.override.yml"]
    again = detect.merge_compose(list(merged.values()))  # idempotent
    assert {s["name"]: s for s in again} == merged


def test_compose_secrets_are_not_kept():
    svc = detect.parse_compose("compose.yml", COMPOSE)[3]
    dumped = json.dumps(svc)
    assert svc["name"] == "backend" and "POSTGRES_PASSWORD" not in dumped and "SECRET_KEY" not in dumped
    assert detect._env_hosts({"DATABASE_URL": "postgres://u:s3cr3t@pg-main.internal:5432/x", "TOKEN": "abc",
                              "CACHE_HOST": "redis", "API": "https://api.stripe.com/v1"}) == \
        ["pg-main", "redis", "api.stripe.com"]


def test_compose_dockerfile_path_and_root_context():
    svcs = detect.parse_compose("compose.yml", "services:\n  backend:\n    build:\n      context: .\n"
                                               "      dockerfile: backend/Dockerfile\n  playwright:\n    build:\n"
                                               "      context: .\n      dockerfile: frontend/Dockerfile.playwright\n")
    assert [(s["build"], s["dockerfile"]) for s in svcs] == [("", "backend/Dockerfile"),
                                                            ("", "frontend/Dockerfile.playwright")]
    merged = detect.merge_compose(svcs)
    assert [s["dev_tool"] for s in merged] == [False, True]


# --------------------------------------------------------------------------- image typing and dev tools
@pytest.mark.parametrize("image, expected", [
    ("postgres:18", ("database", "PostgreSQL", "postgresql")),
    ("pgvector/pgvector:pg16", ("database", "PostgreSQL", "postgresql")),
    ("timescale/timescaledb:latest-pg16", ("database", "TimescaleDB", "postgresql")),
    ("mariadb:11", ("database", "MariaDB", "mysql")),
    ("mysql:8", ("database", "MySQL", "mysql")),
    ("mongo:7", ("database", "MongoDB", "mongodb")),
    ("docker.elastic.co/elasticsearch/elasticsearch:8.15.0", ("database", "Elasticsearch", "elasticsearch")),
    ("opensearchproject/opensearch:2", ("database", "OpenSearch", "elasticsearch")),
    ("bitnami/redis:7.2", ("cache", "Redis", "redis")),
    ("valkey/valkey:8", ("cache", "Valkey", "redis")),
    ("memcached:1.6", ("cache", "Memcached", "memcached")),
    ("rabbitmq:3-management", ("queue", "RabbitMQ", "rabbitmq")),
    ("confluentinc/cp-kafka:7.6.0", ("queue", "Kafka", "kafka")),
    ("nats:2.10", ("queue", "NATS", "nats")),
    ("minio/minio:latest", ("storage", "MinIO", "s3")),
    ("traefik:v3.6", ("gateway", "Traefik", None)),
    ("nginx:1.27-alpine", ("gateway", "NGINX", None)),
    ("caddy:2", ("gateway", "Caddy", None)),
    ("haproxy:3.0", ("gateway", "HAProxy", None)),
    ("envoyproxy/envoy:v1.31", ("gateway", "Envoy", None)),
    ("quay.io/keycloak/keycloak:26.0", ("security", "Keycloak", None)),
    ("prom/prometheus", ("service", "Prometheus", None)),
    ("grafana/grafana:11", ("service", "Grafana", None)),
])
def test_image_typing(image, expected):
    t = detect.image_type(image)
    assert (t["type"], t["name"], t["infra"]) == expected


@pytest.mark.parametrize("image", ["adminer", "dpage/pgadmin4:8", "axllent/mailpit", "mailhog/mailhog",
                                   "maildev/maildev", "mcr.microsoft.com/playwright:v1.49.0-jammy",
                                   "selenium/standalone-chrome", "localstack/localstack", "mongo-express",
                                   "myorg/billing:1.2", "${DOCKER_IMAGE_BACKEND}:${TAG-latest}", ""])
def test_image_typing_unknown_or_dev(image):
    assert detect.image_type(image) is None


def test_dev_tool_rules():
    def svc(name, image="", files=("compose.yml",), profiles=(), dockerfile=None):
        return {"name": name, "image": image, "files": list(files), "profiles": list(profiles), "dockerfile": dockerfile}

    assert detect.is_dev_tool(svc("db-admin", "dpage/pgadmin4"))
    assert detect.is_dev_tool(svc("smtp", "axllent/mailpit"))
    assert detect.is_dev_tool(svc("playwright"))
    assert detect.is_dev_tool(svc("e2e", dockerfile="frontend/Dockerfile.e2e"))
    assert detect.is_dev_tool(svc("worker", profiles=["debug"]))
    assert detect.is_dev_tool(svc("test-runner", files=["docker-compose.override.yml"]))
    assert not detect.is_dev_tool(svc("worker", files=["docker-compose.override.yml"]))  # not a dev-looking name
    assert not detect.is_dev_tool(svc("test-runner"))  # in the base file: kept
    assert not detect.is_dev_tool(svc("grafana", "grafana/grafana"))  # observability is real
    assert not detect.is_dev_tool(svc("db", "postgres:16"))


# --------------------------------------------------------------------------- container ↔ code merging
def base_graph(files, manifests, compose, frameworks=(), infra=(), entry_points=(), edges=(), dockerfiles=()):
    symbols = []
    for f in files:
        symbols.append({"id": "file:" + f, "name": f.split("/")[-1], "file": f, "kind": "file"})
        for i in range(3):
            symbols.append({"id": f"fn{i}:{f}", "name": f"fn{i}", "file": f, "kind": "function"})
    return normalize.validate({
        "format": codegraph.FORMAT, "root_name": "demo",
        "files": [{"path": f, "language": detect.language_of(f), "symbols": 3} for f in files],
        "symbols": symbols,
        "edges": [{"source": f"fn0:{a}", "target": f"fn0:{b}", "relation": r} for a, b, r in edges],
        "detections": {"manifests": list(manifests), "compose_services": list(compose), "frameworks": list(frameworks),
                       "infrastructure": list(infra), "entry_points": list(entry_points),
                       "dockerfiles": list(dockerfiles)},
    })


def manifest(d, kind="python", name="", deps=(), scripts=None):
    return {"path": f"{d}/{'pyproject.toml' if kind == 'python' else 'package.json'}", "dir": d, "kind": kind,
            "name": name, "dependencies": list(deps), "scripts": scripts or {}}


def test_compose_service_merges_with_code_unit_by_dockerfile_and_name():
    files = ["server/app/main.py", "server/app/db.py", "worker/jobs.py", "worker/tasks.py"]
    compose = detect.parse_compose("docker-compose.yml", (
        "services:\n  api:\n    build:\n      context: .\n      dockerfile: server/Dockerfile\n"
        "  worker:\n    image: ghcr.io/acme/worker:1\n    depends_on: [api]\n"
        "  cache:\n    image: valkey/valkey:8\n"))
    g = base_graph(files, [manifest("server", deps=["fastapi"]), manifest("worker", deps=["redis"])],
                   compose, frameworks=[{"name": "FastAPI", "kind": "web-framework", "files": ["server/app/main.py"]}],
                   infra=[{"key": "redis", "label": "Redis", "category": "cache", "type": "cache",
                           "catalog_id": "oss-redis", "technology": "Redis", "files": ["worker/jobs.py"]}],
                   entry_points=[{"path": "worker/jobs.py", "kind": "console-script", "name": "jobs"}])
    nodes, pairs = summary_of(reconstruct.reconstruct(g))
    assert set(nodes) == {"Backend API", "Worker", "Valkey"}
    assert nodes["Backend API"]["props"]["compose_service"] == "api"  # by its Dockerfile's directory
    assert nodes["Worker"]["props"]["compose_service"] == "worker"  # by name (no build in compose)
    assert nodes["Valkey"]["type"] == "cache" and nodes["Valkey"]["props"]["compose_service"] == "cache"
    assert nodes["Valkey"]["catalog_id"] == "oss-redis" and nodes["Valkey"]["props"]["evidence"] == "code+compose"
    assert {("Worker", "Valkey"), ("Worker", "Backend API")} <= pairs


def test_compose_name_similarity():
    files = ["backend/main.py", "backend/db.py", "frontend/src/main.tsx", "frontend/src/app.tsx"]
    compose = detect.parse_compose("compose.yml", "services:\n  api:\n    image: registry.acme.io/api:1\n"
                                                  "  web:\n    image: registry.acme.io/web:1\n    depends_on: [api]\n")
    g = base_graph(files, [manifest("backend", deps=["fastapi"]),
                           manifest("frontend", "node", "web", ["react"], {"dev": "vite"})], compose,
                   frameworks=[{"name": "FastAPI", "kind": "web-framework", "files": ["backend/main.py"]},
                               {"name": "React", "kind": "ui", "files": ["frontend/src/main.tsx"]}])
    nodes, pairs = summary_of(reconstruct.reconstruct(g))
    assert set(nodes) == {"Backend API", "Web App"}
    assert nodes["Backend API"]["props"]["compose_service"] == "api"
    assert nodes["Web App"]["props"]["compose_service"] == "web" and ("Web App", "Backend API") in pairs


def test_libraries_fold_into_importers_and_tooling_is_dropped():
    files = ["apps/web/src/main.tsx", "apps/web/src/page.tsx", "packages/ui/src/button.tsx",
             "packages/ui/src/card.tsx", "packages/api-client/src/sdk.gen.ts", "packages/api-client/src/index.ts",
             "docs/site/index.ts", "storybook/stories/button.stories.tsx", "api/main.py", "api/routes.py"]
    g = base_graph(files, [
        manifest("apps/web", "node", "@acme/web", ["react", "@acme/ui", "@acme/api-client"], {"dev": "vite"}),
        manifest("packages/ui", "node", "@acme/ui", ["react"]),
        manifest("packages/api-client", "node", "@acme/api-client", ["@hey-api/client-fetch"]),
        manifest("storybook", "node", "storybook", ["react", "@storybook/react"], {"dev": "storybook dev"}),
        manifest("api", deps=["fastapi"])], [],
        frameworks=[{"name": "FastAPI", "kind": "web-framework", "files": ["api/main.py"]},
                    {"name": "React", "kind": "ui", "files": ["apps/web/src/main.tsx", "packages/ui/src/button.tsx"]}],
        entry_points=[{"path": "apps/web/src/main.tsx", "kind": "web-client", "name": "main.tsx"}],
        edges=[("apps/web/src/page.tsx", "packages/ui/src/button.tsx", "imports"),
               ("apps/web/src/page.tsx", "packages/api-client/src/index.ts", "imports"),
               ("packages/api-client/src/index.ts", "packages/api-client/src/sdk.gen.ts", "imports")])
    recon = reconstruct.reconstruct(g)
    nodes, pairs = summary_of(recon)
    assert set(nodes) == {"Web App", "Backend API"}
    web = nodes["Web App"]
    assert web["props"]["code_paths"].split(", ")[0] == "apps/web"
    assert set(web["props"]["code_paths"].split(", ")) == {"apps/web", "packages/ui", "packages/api-client"}
    assert pairs == {("Web App", "Backend API")}
    edge = recon["edges"][0]
    assert edge["label"] == "generated OpenAPI client"  # the folded api-client package is the evidence
    excluded = {x["path"]: x["role"] for x in recon["excluded"]}
    assert excluded == {"storybook": "tooling"}  # docs/ is skipped altogether
    hit = impact.code_components(recon, g, ["packages/ui/src/card.tsx"])
    assert hit == [web["id"]]


def test_backend_never_imports_frontend_and_cross_language_is_ignored():
    files = ["backend/main.py", "backend/db.py", "frontend/src/main.tsx", "frontend/src/api.ts",
             "shared/py/models.py", "shared/py/util.py"]
    g = base_graph(files, [manifest("backend", deps=["fastapi"]),
                           manifest("frontend", "node", "web", ["react"], {"dev": "vite"}),
                           manifest("shared/py", name="shared")], [],
                   frameworks=[{"name": "FastAPI", "kind": "web-framework", "files": ["backend/main.py"]},
                               {"name": "React", "kind": "ui", "files": ["frontend/src/main.tsx"]}],
                   edges=[("backend/main.py", "frontend/src/api.ts", "calls"),
                          ("frontend/src/api.ts", "backend/db.py", "calls"),
                          ("backend/db.py", "shared/py/models.py", "imports")])
    recon = reconstruct.reconstruct(g)
    nodes, pairs = summary_of(recon)
    assert pairs == {("Web App", "Backend API")}
    assert "shared/py" in nodes["Backend API"]["props"]["code_paths"]  # folded via the Python import


def test_nginx_config_routes():
    conf = ("upstream api_upstream {\n  server backend:8000;\n}\nserver {\n  location /api/ { proxy_pass "
            "http://api_upstream; }\n  location / { proxy_pass http://frontend:80/; }\n}\n")
    assert detect.parse_proxy_config("proxy/nginx.conf", conf) == \
        {"path": "proxy/nginx.conf", "kind": "nginx", "targets": ["backend", "frontend"]}
    assert detect.parse_proxy_config("Caddyfile", "example.com {\n  reverse_proxy /api/* backend:8000\n}\n")["targets"] \
        == ["backend"]
    files = ["backend/main.py", "backend/db.py", "frontend/src/main.tsx", "frontend/src/app.tsx"]
    compose = detect.parse_compose("compose.yml", "services:\n  proxy:\n    image: nginx:1.27\n"
                                                  "  backend:\n    build: ./backend\n  frontend:\n    build: ./frontend\n")
    g = base_graph(files, [manifest("backend", deps=["fastapi"]),
                           manifest("frontend", "node", "web", ["react"], {"dev": "vite"})], compose,
                   frameworks=[{"name": "FastAPI", "kind": "web-framework", "files": ["backend/main.py"]},
                               {"name": "React", "kind": "ui", "files": ["frontend/src/main.tsx"]}])
    g["detections"]["proxy_routes"] = [detect.parse_proxy_config("proxy/nginx.conf", conf)]
    _, pairs = summary_of(reconstruct.reconstruct(g))
    assert pairs == {("NGINX", "Backend API"), ("NGINX", "Web App"), ("Web App", "NGINX")}


# --------------------------------------------------------------------------- names
@pytest.mark.parametrize("name, ok", [
    ("Orders API", True), ("Billing Worker", True), ("Backend API", True), ("Identity", True),
    ("Web Frontend ApplicationLayer (React, TypeScript)", False), ("Template Automation Hooks Service", False),
    ("FastAPI Backend", False), ("Backend API (FastAPI)", False), ("web app", False), ("Orders/Payments", False),
    ("", False), ("A Very Long Component Name That Goes On", False),
])
def test_valid_name(name, ok):
    assert reconstruct.valid_name(name, "FastAPI · Python") is ok


def test_refine_only_renames_runtime_services_to_valid_names(tmp_path, monkeypatch):
    g = graph_from_repo(make_template_repo(tmp_path / "repo"))
    recon = reconstruct.reconstruct(g)
    ids = {n["name"]: n["id"] for n in recon["nodes"]}
    proposals = [
        {"id": ids["Backend API"], "name": "Template Backend ApplicationLayer (FastAPI)", "description": "The API."},
        {"id": ids["Web App"], "name": "Dashboard", "description": "The SPA."},           # clients keep their name
        {"id": ids["PostgreSQL"], "name": "Application Database Service"},               # infra keeps its name
        {"id": ids["Traefik"], "name": "Traefik Ingress Gateway"},
    ]
    monkeypatch.setattr(llm, "available", lambda org_id=None: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"components": proposals})
    assert reconstruct.refine(recon, g) == "llm"
    names = {n["id"]: n["name"] for n in recon["nodes"]}
    assert set(names.values()) == set(EXPECTED_NODES)
    assert next(n for n in recon["nodes"] if n["id"] == ids["Backend API"])["description"] == "The API."
    proposals[0] = {"id": ids["Backend API"], "name": "Items API"}
    reconstruct.refine(recon, g)
    assert {n["name"] for n in recon["nodes"]} == {"Items API", "Web App", "PostgreSQL", "Traefik"}
