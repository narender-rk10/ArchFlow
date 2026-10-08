"""Deterministic detections over a source tree (no Graphify needed).

Finds languages, package manifests, docker-compose services, Dockerfiles, Kubernetes workloads, Terraform
resources, infrastructure implied by imported libraries (psycopg → PostgreSQL, redis → Redis, openai → LLM
provider, ...) and entry points. Everything is regex/parse based, bounded in file count and size, and never
executes repository code.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

try:  # PyYAML ships with the API's dependencies (uvicorn[standard]); degrade gracefully if absent.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    import tomllib
except ImportError:  # pragma: no cover (Python < 3.11)
    tomllib = None

MAX_FILES = 50_000
MAX_READ = 512 * 1024  # bytes read per file for import scanning / parsing
MAX_FILES_PER_ITEM = 200

NOISE_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", "target", ".next", ".nuxt", "vendor",
              "site-packages", "coverage", ".gradle", ".idea", ".vscode", "graphify-out", ".terraform", "out",
              ".turbo", ".cache", "bower_components", ".claude"}

LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript",
    ".cts": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".rs": "Rust",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".swift": "Swift", ".m": "Objective-C", ".lua": "Lua", ".ex": "Elixir", ".exs": "Elixir", ".dart": "Dart",
    ".vue": "Vue", ".svelte": "Svelte", ".sql": "SQL", ".tf": "Terraform", ".sh": "Shell", ".groovy": "Groovy",
}
CODE_LANGS = {l for l in LANGUAGES.values() if l not in ("SQL", "Terraform", "Shell")}

# ---- infrastructure implied by libraries -----------------------------------------------------------------
# key → (label, category, node type, catalog id, technology)
INFRA: dict[str, tuple[str, str, str, str, str]] = {
    "postgresql": ("PostgreSQL", "database", "database", "oss-postgresql", "PostgreSQL"),
    "mysql": ("MySQL", "database", "database", "oss-mysql", "MySQL"),
    "sqlite": ("SQLite", "database", "database", "", "SQLite"),
    "mongodb": ("MongoDB", "database", "database", "oss-mongodb", "MongoDB"),
    "elasticsearch": ("Elasticsearch", "database", "database", "oss-elasticsearch", "Elasticsearch / OpenSearch"),
    "vector": ("Vector database", "database", "database", "", "Vector database"),
    "redis": ("Redis", "cache", "cache", "oss-redis", "Redis"),
    "memcached": ("Memcached", "cache", "cache", "", "Memcached"),
    "kafka": ("Kafka", "queue", "queue", "oss-kafka", "Apache Kafka"),
    "rabbitmq": ("RabbitMQ", "queue", "queue", "oss-rabbitmq", "RabbitMQ"),
    "nats": ("NATS", "queue", "queue", "oss-nats", "NATS"),
    "sqs": ("Amazon SQS", "queue", "queue", "aws-sqs", "Amazon SQS"),
    "celery": ("Task queue", "queue", "queue", "", "Celery"),
    "s3": ("Object storage", "storage", "storage", "aws-s3", "Amazon S3"),
    "llm": ("LLM provider", "llm", "llm", "", ""),
    "mcp": ("MCP server", "mcp", "mcp", "oss-mcp-server", "Model Context Protocol"),
}
INFRA_CATEGORY_ORDER = ("database", "cache", "queue", "storage", "llm", "mcp")

# (import name pattern, infra key or "framework:<Name>" / "llm:<Provider>" / "client:<Name>")
_PY_LIBS: list[tuple[str, str]] = [
    ("psycopg", "postgresql"), ("psycopg2", "postgresql"), ("psycopg_pool", "postgresql"), ("asyncpg", "postgresql"),
    ("pg8000", "postgresql"), ("sqlalchemy", "postgresql"), ("pymysql", "mysql"), ("MySQLdb", "mysql"),
    ("mysql.connector", "mysql"), ("aiomysql", "mysql"), ("sqlite3", "sqlite"), ("aiosqlite", "sqlite"),
    ("pymongo", "mongodb"), ("motor", "mongodb"), ("mongoengine", "mongodb"), ("beanie", "mongodb"),
    ("elasticsearch", "elasticsearch"), ("opensearchpy", "elasticsearch"),
    ("qdrant_client", "vector"), ("pinecone", "vector"), ("chromadb", "vector"), ("weaviate", "vector"),
    ("pgvector", "vector"),
    ("redis", "redis"), ("aioredis", "redis"), ("valkey", "redis"), ("pymemcache", "memcached"),
    ("nats", "nats"), ("kafka", "kafka"), ("confluent_kafka", "kafka"), ("aiokafka", "kafka"),
    ("pika", "rabbitmq"), ("aio_pika", "rabbitmq"), ("amqp", "rabbitmq"), ("aiormq", "rabbitmq"),
    ("celery", "celery"), ("boto3", "s3"), ("aioboto3", "s3"), ("s3fs", "s3"), ("minio", "s3"),
    ("openai", "llm:OpenAI"), ("anthropic", "llm:Anthropic"), ("litellm", "llm:LiteLLM"),
    ("google.genai", "llm:Gemini"), ("google.generativeai", "llm:Gemini"), ("vertexai", "llm:Gemini"),
    ("mistralai", "llm:Mistral"), ("cohere", "llm:Cohere"), ("ollama", "llm:Ollama"),
    ("langchain_openai", "llm:OpenAI"), ("langchain_anthropic", "llm:Anthropic"),
    ("mcp", "mcp"), ("fastmcp", "mcp"),
    ("fastapi", "framework:FastAPI"), ("flask", "framework:Flask"), ("django", "framework:Django"),
    ("starlette", "framework:Starlette"), ("aiohttp", "framework:aiohttp"), ("tornado", "framework:Tornado"),
    ("sanic", "framework:Sanic"), ("litestar", "framework:Litestar"),
]
_JS_LIBS: list[tuple[str, str]] = [
    ("pg", "postgresql"), ("postgres", "postgresql"), ("pg-promise", "postgresql"), ("@neondatabase/serverless", "postgresql"),
    ("mysql", "mysql"), ("mysql2", "mysql"), ("better-sqlite3", "sqlite"), ("sqlite3", "sqlite"),
    ("mongodb", "mongodb"), ("mongoose", "mongodb"), ("@elastic/elasticsearch", "elasticsearch"),
    ("@qdrant/js-client-rest", "vector"), ("@pinecone-database/pinecone", "vector"),
    ("redis", "redis"), ("ioredis", "redis"), ("@upstash/redis", "redis"),
    ("kafkajs", "kafka"), ("amqplib", "rabbitmq"), ("amqp-connection-manager", "rabbitmq"), ("bullmq", "redis"),
    ("@aws-sdk/client-s3", "s3"), ("aws-sdk", "s3"), ("@aws-sdk/client-sqs", "sqs"),
    ("openai", "llm:OpenAI"), ("@anthropic-ai/sdk", "llm:Anthropic"), ("@google/genai", "llm:Gemini"),
    ("@google/generative-ai", "llm:Gemini"), ("ai", "llm:Vercel AI SDK"),
    ("@modelcontextprotocol/sdk", "mcp"),
    ("express", "framework:Express"), ("fastify", "framework:Fastify"), ("koa", "framework:Koa"),
    ("@nestjs/core", "framework:NestJS"), ("hono", "framework:Hono"), ("@hocuspocus/server", "framework:Hocuspocus"),
    ("next", "client:Next.js"), ("react", "client:React"), ("react-dom", "client:React"), ("vue", "client:Vue"),
    ("@angular/core", "client:Angular"), ("svelte", "client:Svelte"), ("react-native", "client:React Native"),
]
_OTHER_LIBS: list[tuple[str, str]] = [  # Go / Java / Kotlin import prefixes
    ("github.com/lib/pq", "postgresql"), ("github.com/jackc/pgx", "postgresql"), ("org.postgresql", "postgresql"),
    ("github.com/go-sql-driver/mysql", "mysql"), ("com.mysql", "mysql"),
    ("go.mongodb.org/mongo-driver", "mongodb"), ("com.mongodb", "mongodb"),
    ("github.com/redis/go-redis", "redis"), ("github.com/go-redis/redis", "redis"), ("redis.clients.jedis", "redis"),
    ("io.lettuce", "redis"), ("github.com/segmentio/kafka-go", "kafka"), ("github.com/IBM/sarama", "kafka"),
    ("github.com/Shopify/sarama", "kafka"), ("org.apache.kafka", "kafka"), ("github.com/rabbitmq/amqp091-go", "rabbitmq"),
    ("com.rabbitmq", "rabbitmq"), ("github.com/aws/aws-sdk-go", "s3"), ("software.amazon.awssdk.services.s3", "s3"),
    ("github.com/sashabaranov/go-openai", "llm:OpenAI"), ("github.com/anthropics/anthropic-sdk-go", "llm:Anthropic"),
    ("org.springframework", "framework:Spring"), ("github.com/gin-gonic/gin", "framework:Gin"),
    ("github.com/labstack/echo", "framework:Echo"), ("github.com/gofiber/fiber", "framework:Fiber"),
    ("net/http", "framework:net/http"),
]

LLM_CATALOG = {"OpenAI": "saas-openai", "Anthropic": "saas-anthropic", "Gemini": "saas-gemini", "LiteLLM": "oss-litellm"}

# ---- container images ------------------------------------------------------------------------------------
# Dev/test tooling that is never part of the runtime architecture (DB admin UIs, fake SMTP servers, browser
# test runners, cloud emulators). Matched against image words and compose service names.
DEV_TOOL_WORDS = ("adminer", "pgadmin", "pgadmin4", "phpmyadmin", "mongo-express", "redis-commander", "redisinsight",
                  "mailpit", "mailhog", "maildev", "smtp4dev", "mailcatcher", "playwright", "selenium", "cypress",
                  "standalone-chrome", "standalone-firefox", "localstack", "wiremock", "kafka-ui", "kafdrop",
                  "akhq", "dbgate", "cloudbeaver")
# (image words, node type, display name, catalog id, infra key or None). First match wins; datastores carry an
# infra key so they merge with the same infrastructure detected from client libraries.
IMAGE_TYPES: list[tuple[tuple[str, ...], str, str, str, str | None]] = [
    (("timescaledb",), "database", "TimescaleDB", "oss-postgresql", "postgresql"),
    (("postgres", "postgresql", "postgis", "pgvector"), "database", "PostgreSQL", "oss-postgresql", "postgresql"),
    (("mariadb",), "database", "MariaDB", "oss-mysql", "mysql"),
    (("mysql", "percona"), "database", "MySQL", "oss-mysql", "mysql"),
    (("mongo", "mongodb"), "database", "MongoDB", "oss-mongodb", "mongodb"),
    (("elasticsearch",), "database", "Elasticsearch", "oss-elasticsearch", "elasticsearch"),
    (("opensearch",), "database", "OpenSearch", "oss-elasticsearch", "elasticsearch"),
    (("qdrant",), "database", "Qdrant", "oss-qdrant", "vector"),
    (("weaviate",), "database", "Weaviate", "", "vector"),
    (("chroma", "chromadb"), "database", "Chroma", "", "vector"),
    (("valkey",), "cache", "Valkey", "oss-redis", "redis"),
    (("redis", "redis-stack", "redis-stack-server", "keydb"), "cache", "Redis", "oss-redis", "redis"),
    (("memcached",), "cache", "Memcached", "", "memcached"),
    (("rabbitmq",), "queue", "RabbitMQ", "oss-rabbitmq", "rabbitmq"),
    (("redpanda",), "queue", "Redpanda", "oss-kafka", "kafka"),
    (("kafka", "cp-kafka"), "queue", "Kafka", "oss-kafka", "kafka"),
    (("nats",), "queue", "NATS", "oss-nats", "nats"),
    (("minio",), "storage", "MinIO", "oss-minio", "s3"),
    (("traefik",), "gateway", "Traefik", "oss-traefik", None),
    (("nginx", "nginx-unprivileged", "openresty"), "gateway", "NGINX", "oss-nginx", None),
    (("caddy",), "gateway", "Caddy", "", None),
    (("haproxy",), "gateway", "HAProxy", "", None),
    (("envoy",), "gateway", "Envoy", "", None),
    (("kong",), "gateway", "Kong", "oss-kong", None),
    (("keycloak",), "security", "Keycloak", "oss-keycloak", None),
    (("authelia",), "security", "Authelia", "", None),
    (("vault",), "security", "Vault", "oss-vault", None),
    (("prometheus",), "service", "Prometheus", "oss-prometheus-grafana", None),
    (("grafana",), "service", "Grafana", "oss-prometheus-grafana", None),
    (("loki",), "service", "Loki", "oss-prometheus-grafana", None),
    (("jaeger", "jaegertracing"), "service", "Jaeger", "", None),
    (("otel-collector", "opentelemetry-collector", "opentelemetry-collector-contrib"), "service",
     "OpenTelemetry Collector", "", None),
]
# compose file names that only apply to local development / tests
_DEV_COMPOSE_FILE = re.compile(r"override|(^|[.\-_])(dev|local|test|tests|e2e|debug|ci)([.\-_]|$)", re.I)
_DEV_SERVICE_NAME = re.compile(r"(^|[-_])(test|tests|e2e|mock|mocks|stub|debug|dev|tools?|admin|mail|smtp|seed|"
                               r"storybook|docs|playwright|cypress|selenium)([-_]|$)", re.I)
_DEV_PROFILES = {"dev", "debug", "test", "tests", "e2e", "tools", "tooling", "admin"}


def image_words(image: str) -> list[str]:
    """``registry/org/name:tag`` → path segments and their dash-separated parts (lowercase, no tag/digest)."""
    img = (image or "").lower().split("@")[0]
    img = re.sub(r"\$\{[^}]*\}", "", img)
    segs = [s for s in img.split("/") if s]
    if segs:
        segs[-1] = segs[-1].split(":")[0]
    words: list[str] = []
    for s in segs[1:] if len(segs) > 1 and ("." in segs[0] or ":" in segs[0]) else segs:  # drop a registry host
        words.append(s)
        words.extend(p for p in re.split(r"[-_.]", s) if p)
    return words


def image_type(image: str) -> dict[str, Any] | None:
    """Known images → {type, name, catalog_id, infra}; None for unknown or dev-tool images."""
    words = image_words(image)
    if not words or any(w in DEV_TOOL_WORDS for w in words):
        return None
    for keys, ntype, name, cid, infra in IMAGE_TYPES:
        if any(k in words for k in keys):
            return {"type": ntype, "name": name, "catalog_id": cid, "infra": infra}
    return None


def is_dev_tool(svc: dict[str, Any]) -> bool:
    """Dev/test tooling: a known tool image or name, a dev profile, or a dev-looking service that only exists in
    override/dev compose files."""
    words = image_words(svc.get("image", "")) + [svc.get("name", "").lower()]
    words += [p for p in re.split(r"[-_]", svc.get("name", "").lower()) if p]
    dockerfile = PurePosixPath(svc.get("dockerfile") or "").name.lower()
    if any(w in DEV_TOOL_WORDS for w in words) or re.search(r"playwright|cypress|selenium|e2e", dockerfile):
        return True
    if _DEV_PROFILES & {str(p).lower() for p in svc.get("profiles") or []}:
        return True
    files = svc.get("files") or ([svc["file"]] if svc.get("file") else [])
    only_dev = bool(files) and all(_DEV_COMPOSE_FILE.search(PurePosixPath(f).name) for f in files)
    return only_dev and bool(_DEV_SERVICE_NAME.search(svc.get("name", "")))
# Terraform resource types → infra key
_TF_INFRA = [
    (r"^aws_(db_instance|rds_cluster)|^azurerm_(postgresql|mssql)|^google_sql_database", "postgresql"),
    (r"^aws_elasticache|^azurerm_redis|^google_redis", "redis"), (r"^aws_s3_bucket$|^azurerm_storage_account$|^google_storage_bucket$", "s3"),
    (r"^aws_sqs_queue$", "sqs"), (r"^aws_msk_cluster$", "kafka"), (r"^aws_dynamodb_table$", "mongodb"),
]


# --------------------------------------------------------------------------- helpers
def rel(path: Path, root: Path) -> str:
    return PurePosixPath(*path.relative_to(root).parts).as_posix()


def walk(root: Path, max_files: int = MAX_FILES) -> Iterator[Path]:
    """Files under root, skipping vendored/build/VCS directories and symlinks that leave the tree."""
    count = 0
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in NOISE_DIRS and not d.endswith(".egg-info")
                             and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_symlink() and not os.path.realpath(p).startswith(root_real):
                continue
            count += 1
            if count > max_files:
                return
            yield p


def read_text(path: Path, limit: int = MAX_READ) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|testdata|fixtures|e2e)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|"
                        r"\.(test|spec)\.[jt]sx?$|(^|/)conftest\.py$")


def is_test_path(path: str) -> bool:
    """Tests, fixtures and specs: excluded from infrastructure evidence and entry points."""
    return bool(_TEST_PATH.search(path))


_CODEGEN_PATH = re.compile(r"\.gen\.[cm]?[jt]sx?$|\.generated\.\w+$|(^|/)(__generated__|generated|gen)/|_pb2(_grpc)?\.pyi?$|"
                           r"\.pb(\.gw)?\.go$|_grpc\.pb\.go$")
# Code generated from an OpenAPI spec (hey-api/openapi-ts, openapi-typescript-codegen, orval, openapi-generator):
# a client app that ships one calls the backend over HTTP.
_OPENAPI_CLIENT_FILE = re.compile(r"(^|/)(sdk|services|schemas|types|client)\.gen\.ts$|(^|/)core/(OpenAPI|request|ApiError)\.ts$|"
                                  r"(^|/)openapi-ts\.config\.[cm]?[jt]s$|(^|/)orval\.config\.[cm]?[jt]s$")
OPENAPI_CLIENT_DEPS = {"@hey-api/openapi-ts", "@hey-api/client-axios", "@hey-api/client-fetch", "openapi-typescript-codegen",
                       "@openapitools/openapi-generator-cli", "orval", "openapi-typescript", "openapi-fetch"}


def is_codegen_path(path: str) -> bool:
    """Generated code (``*.gen.ts``, ``generated/``, protobuf stubs): never evidence of a dependency direction."""
    return bool(_CODEGEN_PATH.search(path))


def is_openapi_client_file(path: str) -> bool:
    return bool(_OPENAPI_CLIENT_FILE.search(path))


def language_of(path: str) -> str | None:
    return LANGUAGES.get(PurePosixPath(path).suffix.lower())


_PY_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w\.]*)\s+import|import\s+([A-Za-z_][\w\.]*(?:\s*,\s*[A-Za-z_][\w\.]*)*))",
                        re.M)
_JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\brequire\(\s*|\bimport\s*\(\s*|^\s*import\s+)['"]([^'"\n]+)['"]""", re.M)
_JVM_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w\.]+)", re.M)
_GO_IMPORT = re.compile(r'"([\w\.\-/]+)"')


def imported_modules(path: str, text: str) -> set[str]:
    """Module names a source file imports (dotted for Python, package names for JS, paths for Go/JVM)."""
    lang = language_of(path)
    out: set[str] = set()
    if lang == "Python":
        for frm, imp in _PY_IMPORT.findall(text):
            for name in ([frm] if frm else [x.strip() for x in imp.split(",")]):
                if name and not name.startswith("."):
                    out.add(name)
    elif lang in ("TypeScript", "JavaScript", "Vue", "Svelte"):
        for spec in _JS_IMPORT.findall(text):
            if spec.startswith((".", "/", "~", "#")) or ":" in spec and not spec.startswith("node:"):
                continue
            parts = spec.split("/")
            out.add("/".join(parts[:2]) if spec.startswith("@") else parts[0])
    elif lang in ("Java", "Kotlin", "Scala"):
        out.update(_JVM_IMPORT.findall(text))
    elif lang == "Go":
        block = re.findall(r"import\s*\((.*?)\)", text, re.S)
        single = re.findall(r'^\s*import\s+(?:\w+\s+)?("[^"]+")', text, re.M)
        for chunk in block + single:
            out.update(_GO_IMPORT.findall(chunk))
    return out


def libs_for(path: str, modules: set[str]) -> set[str]:
    """Infra / framework tags for a file's imported modules."""
    lang = language_of(path)
    table = _PY_LIBS if lang == "Python" else _JS_LIBS if lang in ("TypeScript", "JavaScript", "Vue", "Svelte") else _OTHER_LIBS
    tags: set[str] = set()
    for mod in modules:
        for name, tag in table:
            if lang == "Python":
                if mod == name or mod.startswith(name + "."):
                    tags.add(tag)
            elif lang in ("TypeScript", "JavaScript", "Vue", "Svelte"):
                if mod == name:
                    tags.add(tag)
            elif mod == name or mod.startswith(name + "/") or mod.startswith(name + "."):
                tags.add(tag)
    return tags


def _load_yaml_docs(text: str) -> list[Any]:
    if yaml is None:
        return []
    try:
        return [d for d in yaml.safe_load_all(text) if d is not None]
    except Exception:
        return []


def _toml(text: str) -> dict[str, Any]:
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(text)
    except Exception:
        return {}


def _req_name(spec: str) -> str:
    return re.split(r"[\s\[<>=!~;@]", spec.strip(), maxsplit=1)[0].lower()


def image_infra(image: str) -> str | None:
    """Infra key (postgresql, redis, ...) of a datastore image, else None."""
    return (image_type(image) or {}).get("infra")


# --------------------------------------------------------------------------- manifests
def parse_manifest(path: str, text: str) -> dict[str, Any] | None:
    name = PurePosixPath(path).name.lower()
    d = str(PurePosixPath(path).parent)
    d = "" if d == "." else d
    base = {"path": path, "dir": d}
    if name == "pyproject.toml":
        data = _toml(text)
        proj = data.get("project", {}) or {}
        deps = [_req_name(x) for x in proj.get("dependencies", []) or [] if isinstance(x, str)]
        for group in (proj.get("optional-dependencies") or {}).values():
            deps += [_req_name(x) for x in group if isinstance(x, str)]
        poetry = ((data.get("tool") or {}).get("poetry") or {})
        deps += [k.lower() for k in (poetry.get("dependencies") or {}) if k.lower() != "python"]
        scripts = proj.get("scripts") or poetry.get("scripts") or {}
        return {**base, "kind": "python", "name": proj.get("name") or poetry.get("name") or "",
                "dependencies": sorted(set(deps)), "scripts": {k: str(v) for k, v in scripts.items()}}
    if name.startswith("requirements") and name.endswith(".txt"):
        deps = [_req_name(l) for l in text.splitlines() if l.strip() and not l.strip().startswith(("#", "-"))]
        return {**base, "kind": "python", "name": "", "dependencies": sorted(set(filter(None, deps))), "scripts": {}}
    if name == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        deps = list((data.get("dependencies") or {}).keys()) + list((data.get("devDependencies") or {}).keys())
        scripts = data.get("scripts") or {}
        return {**base, "kind": "node", "name": str(data.get("name") or ""), "dependencies": sorted(set(deps)),
                "scripts": {k: str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {},
                "main": str(data.get("main") or ""), "workspaces": bool(data.get("workspaces"))}
    if name == "go.mod":
        mod = re.search(r"^module\s+(\S+)", text, re.M)
        deps = re.findall(r"^\s*([\w\.\-]+\.[a-z]+/[\w\.\-/]+)\s+v", text, re.M)
        return {**base, "kind": "go", "name": mod.group(1) if mod else "", "dependencies": sorted(set(deps)), "scripts": {}}
    if name == "pom.xml":
        art = re.search(r"<artifactId>([^<]+)</artifactId>", text)
        deps = re.findall(r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>", text)
        return {**base, "kind": "java", "name": art.group(1) if art else "",
                "dependencies": sorted({f"{g}:{a}" for g, a in deps}), "scripts": {}}
    if name in ("build.gradle", "build.gradle.kts"):
        deps = re.findall(r"""(?:implementation|api|compile|runtimeOnly)\s*\(?\s*['"]([^'"]+)['"]""", text)
        return {**base, "kind": "java", "name": "", "dependencies": sorted({":".join(x.split(":")[:2]) for x in deps}),
                "scripts": {}}
    if name == "cargo.toml":
        data = _toml(text)
        return {**base, "kind": "rust", "name": (data.get("package") or {}).get("name", ""),
                "dependencies": sorted((data.get("dependencies") or {}).keys()), "scripts": {}}
    return None


MANIFEST_NAMES = {"pyproject.toml", "package.json", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "cargo.toml"}


def is_manifest(name: str) -> bool:
    n = name.lower()
    return n in MANIFEST_NAMES or (n.startswith("requirements") and n.endswith(".txt"))


# --------------------------------------------------------------------------- compose / docker / k8s / terraform
def _join(base: str, rel_path: str) -> str:
    """Normalise ``base/rel_path`` to a POSIX path relative to the repository root ('' for the root)."""
    parts: list[str] = []
    for part in (PurePosixPath(base) / str(rel_path).replace("\\", "/")).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", "", "/"):
            parts.append(part)
    return "/".join(parts)


_URL_HOST = re.compile(r"://(?:[^@/]{0,200}@)?([A-Za-z][A-Za-z0-9_.\-]*)")
_HOST_KEY = re.compile(r"(HOST|HOSTNAME|SERVER|ADDR|ADDRESS|ENDPOINT)$", re.I)


def _env_hosts(env: Any) -> list[str]:
    """Hostnames referenced by environment values (``postgresql://u:p@db:5432/x`` → db, ``SMTP_HOST=mailpit`` →
    mailpit). Only host names are kept — never values, which may be secrets."""
    pairs: list[tuple[str, str]] = []
    if isinstance(env, dict):
        pairs = [(str(k), str(v)) for k, v in env.items() if v is not None]
    elif isinstance(env, list):
        pairs = [tuple(str(x).split("=", 1)) for x in env if isinstance(x, str) and "=" in x]  # type: ignore[misc]
    hosts: list[str] = []
    for key, value in pairs:
        found = _URL_HOST.findall(value)
        if not found and _HOST_KEY.search(key) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]*", value.strip()):
            found = [value.strip()]
        for h in found:
            h = h.split(".")[0] if not re.search(r"\.(com|net|org|io|dev|ai|cloud)$", h) else h
            if h and h not in hosts and h.lower() not in ("localhost", "0", "127"):
                hosts.append(h)
    return hosts[:20]


def parse_compose(path: str, text: str) -> list[dict[str, Any]]:
    """Services of one compose file (one entry per service; see ``merge_compose`` for the whole project)."""
    out = []
    for doc in _load_yaml_docs(text):
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        base_dir = str(PurePosixPath(path).parent)
        for name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            build = svc.get("build")
            context = build.get("context") if isinstance(build, dict) else build if isinstance(build, str) else None
            build_dir = dockerfile = None
            if build is not None:
                build_dir = _join(base_dir, str(context or "."))
                df = build.get("dockerfile") if isinstance(build, dict) else None
                if isinstance(df, str) and df and not df.startswith("/"):
                    dockerfile = _join(build_dir, df)
            deps = svc.get("depends_on") or []
            deps = list(deps.keys()) if isinstance(deps, dict) else [str(x) for x in deps if isinstance(x, (str, int))]
            ports = [str(p) for p in (svc.get("ports") or []) if isinstance(p, (str, int))]
            labels = svc.get("labels") or {}
            label_keys = ([str(k) + ("=" + str(v) if str(v).lower() in ("true", "false") else "")
                           for k, v in labels.items()] if isinstance(labels, dict) else
                          [str(x).split("=", 1)[0] + ("=" + str(x).split("=", 1)[1]
                                                      if str(x).split("=", 1)[-1].lower() in ("true", "false") else "")
                           for x in labels if isinstance(x, str)] if isinstance(labels, list) else [])
            profiles = [str(p) for p in (svc.get("profiles") or []) if isinstance(p, str)]
            image = str(svc.get("image") or "")
            out.append({"name": str(name), "file": path, "files": [path], "image": image, "build": build_dir,
                        "dockerfile": dockerfile, "depends_on": deps, "ports": ports[:10],
                        "labels": label_keys[:40], "env_hosts": _env_hosts(svc.get("environment")),
                        "profiles": profiles[:10], "infra": None if build_dir is not None else image_infra(image)})
    return out


def _compose_rank(path: str) -> tuple[int, int, str]:
    """docker compose order: the base file, then ``*.override.*``, then the other (deploy/prod/...) files."""
    name = PurePosixPath(path).name.lower()
    rank = 0 if re.fullmatch(r"(docker-)?compose\.ya?ml", name) else 1 if "override" in name else 2
    return (path.count("/"), rank, path)


def merge_compose(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge services with the same name across compose files, like ``docker compose -f a -f b`` does: later
    files override scalars (image, build, dockerfile) and extend lists (ports, depends_on, labels, ...).
    ``files`` lists every file that defines the service. Idempotent (merged entries merge to themselves)."""
    merged: dict[str, dict[str, Any]] = {}
    for e in sorted(entries, key=lambda e: _compose_rank(str(e.get("file") or ""))):
        name = str(e.get("name") or "")
        if not name:
            continue
        cur = merged.get(name)
        if cur is None:
            cur = merged[name] = {"name": name, "file": e.get("file"), "files": [], "image": "", "build": None,
                                  "dockerfile": None, "depends_on": [], "ports": [], "labels": [], "env_hosts": [],
                                  "profiles": [], "infra": None}
        for f in e.get("files") or ([e["file"]] if e.get("file") else []):
            if f not in cur["files"]:
                cur["files"].append(f)
        if e.get("image"):
            cur["image"] = e["image"]
        if e.get("build") is not None:
            cur["build"] = e["build"]
        if e.get("dockerfile"):
            cur["dockerfile"] = e["dockerfile"]
        for key in ("depends_on", "ports", "labels", "env_hosts", "profiles"):
            for v in e.get(key) or []:
                if v not in cur[key]:
                    cur[key].append(v)
    for svc in merged.values():
        svc["infra"] = None if svc["build"] is not None else image_infra(svc["image"])
        svc["dev_tool"] = is_dev_tool(svc)
    return list(merged.values())


def traefik_routed(svc: dict[str, Any]) -> bool:
    """A service that Traefik routes to (Docker provider labels)."""
    labels = svc.get("labels") or []
    return any(str(l).startswith("traefik.http.routers.") for l in labels) or "traefik.enable=true" in labels


# --------------------------------------------------------------------------- reverse-proxy configs
_NGINX_TARGET = re.compile(r"\b(?:proxy_pass|grpc_pass|uwsgi_pass|fastcgi_pass)\s+(?:https?://|grpc://)?([A-Za-z][\w.\-]*)")
_UPSTREAM = re.compile(r"upstream\s+([\w.\-]+)\s*\{([^}]*)\}", re.S)
_CADDY_TARGET = re.compile(r"\breverse_proxy\s+(?:[/\w*\-.]+\s+)?(?:https?://)?([A-Za-z][\w.\-]*)")
_TRAEFIK_URL = re.compile(r"""\burl\s*[:=]\s*["']?https?://([A-Za-z][\w.\-]*)""")


def parse_proxy_config(path: str, text: str) -> dict[str, Any] | None:
    """Upstream hosts a reverse-proxy config routes to (nginx ``proxy_pass``/``upstream``, Caddy
    ``reverse_proxy``, Traefik file-provider ``url``)."""
    name = PurePosixPath(path).name.lower()
    if name.startswith("caddyfile"):
        kind, targets = "caddy", _CADDY_TARGET.findall(text)
    elif "traefik" in path.lower() and name.endswith((".yml", ".yaml", ".toml")):
        kind, targets = "traefik", _TRAEFIK_URL.findall(text)
    elif name.endswith(".conf") or name.endswith(".conf.template"):
        if "proxy_pass" not in text and "upstream" not in text:
            return None
        kind = "nginx"
        targets = _NGINX_TARGET.findall(text)
        upstreams = dict(_UPSTREAM.findall(text))
        expanded: list[str] = []
        for t in targets:
            if t in upstreams:
                expanded += re.findall(r"server\s+([A-Za-z][\w.\-]*)", upstreams[t])
            else:
                expanded.append(t)
        targets = expanded
    else:
        return None
    hosts = []
    for t in targets:
        if t.lower() not in ("localhost", "127") and "$" not in t and t not in hosts:
            hosts.append(t)
    return {"path": path, "kind": kind, "targets": hosts[:30]} if hosts else None


def parse_dockerfile(path: str, text: str) -> dict[str, Any]:
    froms = re.findall(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", text, re.M | re.I)
    expose = re.findall(r"^\s*EXPOSE\s+(.+)$", text, re.M | re.I)
    cmd = re.findall(r"^\s*(?:CMD|ENTRYPOINT)\s+(.+)$", text, re.M | re.I)
    parent = str(PurePosixPath(path).parent)
    return {"path": path, "dir": "" if parent == "." else parent,
            "base_images": froms[:5], "expose": " ".join(expose).split()[:10], "cmd": cmd[-1].strip()[:200] if cmd else ""}


K8S_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Service", "Ingress", "CronJob", "Job"}


def parse_k8s(path: str, text: str) -> list[dict[str, Any]]:
    if "apiVersion" not in text or "kind" not in text:
        return []
    out = []
    for doc in _load_yaml_docs(text):
        if not isinstance(doc, dict) or doc.get("kind") not in K8S_KINDS:
            continue
        meta = doc.get("metadata") or {}
        images: list[str] = []

        def find_images(obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k == "image" and isinstance(v, str):
                        images.append(v)
                    else:
                        find_images(v)
            elif isinstance(obj, list):
                for v in obj:
                    find_images(v)

        find_images(doc.get("spec"))
        out.append({"kind": doc["kind"], "name": str(meta.get("name", "")) if isinstance(meta, dict) else "",
                    "file": path, "images": images[:10]})
    return out


_TF_RESOURCE = re.compile(r'^\s*resource\s+"([\w\-]+)"\s+"([\w\-]+)"', re.M)


def parse_terraform(path: str, text: str) -> list[dict[str, Any]]:
    return [{"type": t, "name": n, "file": path} for t, n in _TF_RESOURCE.findall(text)]


def tf_infra(resource_type: str) -> str | None:
    for pattern, key in _TF_INFRA:
        if re.search(pattern, resource_type):
            return key
    return None


# --------------------------------------------------------------------------- entry points
_PY_MAIN = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", re.M)
_PY_APP = re.compile(r"\b(FastAPI|Flask|Starlette|Litestar|Sanic)\s*\(")
_GO_MAIN = re.compile(r"^package\s+main\b", re.M)
_JS_LISTEN = re.compile(r"\.listen\s*\(")


def entry_points_for(path: str, text: str) -> list[dict[str, Any]]:
    lang = language_of(path)
    name = PurePosixPath(path).name
    out = []
    if lang == "Python":
        if _PY_MAIN.search(text) or name == "__main__.py":
            out.append({"path": path, "kind": "python-main", "name": name})
        if m := _PY_APP.search(text):
            out.append({"path": path, "kind": "web-app", "name": m.group(1)})
        if name == "manage.py" and "django" in text.lower():
            out.append({"path": path, "kind": "web-app", "name": "Django"})
    elif lang == "Go" and _GO_MAIN.search(text) and re.search(r"^func\s+main\s*\(", text, re.M):
        out.append({"path": path, "kind": "go-main", "name": name})
    elif lang in ("TypeScript", "JavaScript"):
        stem = PurePosixPath(path).stem
        if stem in ("main", "index", "server", "app") and _JS_LISTEN.search(text):
            out.append({"path": path, "kind": "server", "name": name})
        elif stem == "main" and re.search(r"createRoot|ReactDOM\.render|createApp\(", text):
            out.append({"path": path, "kind": "web-client", "name": name})
    elif lang in ("Java", "Kotlin") and re.search(r"public\s+static\s+void\s+main\s*\(|^fun\s+main\s*\(", text, re.M):
        out.append({"path": path, "kind": "jvm-main", "name": name})
    return out


# --------------------------------------------------------------------------- the whole tree
def detect(root: Path) -> dict[str, Any]:
    """All deterministic detections for the tree at ``root`` (paths are POSIX, relative to root)."""
    root = Path(root).resolve()
    languages: dict[str, int] = {}
    manifests: list[dict[str, Any]] = []
    compose: list[dict[str, Any]] = []
    dockerfiles: list[dict[str, Any]] = []
    k8s: list[dict[str, Any]] = []
    terraform: list[dict[str, Any]] = []
    entry_points: list[dict[str, Any]] = []
    proxies: list[dict[str, Any]] = []
    tags_by_file: dict[str, set[str]] = {}
    for p in walk(root):
        path = rel(p, root)
        name = p.name
        lname = name.lower()
        lang = language_of(path)
        if lang:
            languages[lang] = languages.get(lang, 0) + 1
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > 5 * MAX_READ and not lang:
            continue
        if is_manifest(name):
            if m := parse_manifest(path, read_text(p)):
                manifests.append(m)
        elif re.fullmatch(r"(docker-)?compose([.\-][\w\-]+)?\.ya?ml", lname):
            compose.extend(parse_compose(path, read_text(p)))
        elif lname == "dockerfile" or lname.startswith("dockerfile.") or lname.endswith(".dockerfile"):
            dockerfiles.append(parse_dockerfile(path, read_text(p)))
        elif lname.endswith((".yaml", ".yml")):
            text = read_text(p)
            k8s.extend(parse_k8s(path, text))
            if "traefik" in path.lower() and (cfg := parse_proxy_config(path, text)):
                proxies.append(cfg)
        elif lname.endswith(".tf"):
            terraform.extend(parse_terraform(path, read_text(p)))
        elif lname.endswith((".conf", ".conf.template")) or lname.startswith("caddyfile") or \
                (lname.endswith(".toml") and "traefik" in path.lower()):
            if cfg := parse_proxy_config(path, read_text(p)):
                proxies.append(cfg)
        if lang in CODE_LANGS and not is_test_path(path):
            text = read_text(p)
            tags = libs_for(path, imported_modules(path, text))
            if tags:
                tags_by_file[path] = tags
            entry_points.extend(entry_points_for(path, text))

    for m in manifests:
        for script, target in (m.get("scripts") or {}).items():
            if m["kind"] == "python":
                entry_points.append({"path": m["path"], "kind": "console-script", "name": f"{script} = {target}"})
            elif script in ("start", "dev", "serve"):
                entry_points.append({"path": m["path"], "kind": f"npm-{script}", "name": target[:120]})

    compose = merge_compose(compose)
    return {
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "manifests": manifests,
        "compose_services": compose,
        "dockerfiles": dockerfiles,
        "k8s": k8s,
        "terraform": terraform[:500],
        "proxy_routes": proxies[:50],
        "infrastructure": infrastructure(tags_by_file, compose, k8s, terraform),
        "frameworks": frameworks(tags_by_file),
        "entry_points": entry_points[:200],
    }


def infrastructure(tags_by_file: dict[str, set[str]], compose: list[dict[str, Any]], k8s: list[dict[str, Any]],
                   terraform: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Infra items with the evidence that implies them: files that import a client library, compose/k8s images,
    Terraform resources."""
    items: dict[str, dict[str, Any]] = {}

    def item(key: str) -> dict[str, Any]:
        if key not in items:
            label, category, ntype, cid, tech = INFRA[key]
            items[key] = {"key": key, "label": label, "category": category, "type": ntype, "catalog_id": cid,
                          "technology": tech, "providers": [], "files": [], "evidence": []}
        return items[key]

    for path, tags in sorted(tags_by_file.items()):
        for tag in tags:
            if tag.startswith("llm:"):
                it = item("llm")
                provider = tag.split(":", 1)[1]
                if provider not in it["providers"]:
                    it["providers"].append(provider)
            elif tag in INFRA:
                it = item(tag)
            else:
                continue
            if len(it["files"]) < MAX_FILES_PER_ITEM:
                it["files"].append(path)
    for svc in compose:
        if svc.get("infra") and not svc.get("dev_tool"):
            ev = f"compose service '{svc['name']}' ({svc['image']})"
            if ev not in item(svc["infra"])["evidence"]:
                item(svc["infra"])["evidence"].append(ev)
    for w in k8s:
        for img in w.get("images", []):
            if key := image_infra(img):
                item(key)["evidence"].append(f"k8s {w['kind']} '{w['name']}' ({img})")
    for r in terraform:
        if key := tf_infra(r["type"]):
            item(key)["evidence"].append(f"terraform {r['type']}.{r['name']}")

    if "llm" in items:
        it = items["llm"]
        provs = it["providers"]
        it["technology"] = ", ".join(provs)
        it["catalog_id"] = LLM_CATALOG.get("LiteLLM" if "LiteLLM" in provs else provs[0], "") if provs else ""
        if "LiteLLM" in provs:  # the gateway fronts the other providers
            it["label"] = "LiteLLM"
        elif len(provs) == 1:
            it["label"] = f"{provs[0]} API"
    order = {c: i for i, c in enumerate(INFRA_CATEGORY_ORDER)}
    out = sorted(items.values(), key=lambda x: (order.get(x["category"], 9), x["label"]))
    for it in out:
        it["evidence"] = it["evidence"][:20]
    return out


def frameworks(tags_by_file: dict[str, set[str]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path, tags in sorted(tags_by_file.items()):
        for tag in tags:
            if tag.startswith(("framework:", "client:")):
                kind, name = tag.split(":", 1)
                f = found.setdefault(name, {"name": name, "kind": "web-framework" if kind == "framework" else "ui",
                                            "files": []})
                if len(f["files"]) < MAX_FILES_PER_ITEM:
                    f["files"].append(path)
    return sorted(found.values(), key=lambda f: (-len(f["files"]), f["name"]))
