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
    "kafka": ("Kafka", "queue", "queue", "oss-kafka", "Apache Kafka"),
    "rabbitmq": ("RabbitMQ", "queue", "queue", "oss-rabbitmq", "RabbitMQ"),
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
    ("redis", "redis"), ("aioredis", "redis"), ("valkey", "redis"),
    ("kafka", "kafka"), ("confluent_kafka", "kafka"), ("aiokafka", "kafka"),
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

# docker-compose / Kubernetes images → infra key
_IMAGE_INFRA = [
    (r"(^|/)(postgres|postgis|pgvector|timescaledb)", "postgresql"), (r"(^|/)(mysql|mariadb)", "mysql"),
    (r"(^|/)mongo", "mongodb"), (r"(^|/)(redis|valkey|keydb)", "redis"), (r"(^|/)rabbitmq", "rabbitmq"),
    (r"kafka|redpanda", "kafka"), (r"(^|/)(elasticsearch|opensearch)", "elasticsearch"), (r"(^|/)minio", "s3"),
    (r"(^|/)qdrant|weaviate|chroma", "vector"),
]
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
    img = (image or "").lower().split("@")[0].split(":")[0]
    for pattern, key in _IMAGE_INFRA:
        if re.search(pattern, img):
            return key
    return None


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
def parse_compose(path: str, text: str) -> list[dict[str, Any]]:
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
            build_dir = None
            if context:
                p = PurePosixPath(base_dir) / str(context).replace("\\", "/")
                parts: list[str] = []
                for part in p.parts:
                    if part == "..":
                        if parts:
                            parts.pop()
                    elif part not in (".", ""):
                        parts.append(part)
                build_dir = "/".join(parts)
            deps = svc.get("depends_on") or []
            deps = list(deps.keys()) if isinstance(deps, dict) else [str(x) for x in deps if isinstance(x, (str, int))]
            ports = [str(p) for p in (svc.get("ports") or []) if isinstance(p, (str, int))]
            image = str(svc.get("image") or "")
            out.append({"name": str(name), "file": path, "image": image, "build": build_dir, "depends_on": deps,
                        "ports": ports[:10], "infra": None if build_dir is not None else image_infra(image)})
    return out


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
            k8s.extend(parse_k8s(path, read_text(p)))
        elif lname.endswith(".tf"):
            terraform.extend(parse_terraform(path, read_text(p)))
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

    return {
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "manifests": manifests,
        "compose_services": compose,
        "dockerfiles": dockerfiles,
        "k8s": k8s,
        "terraform": terraform[:500],
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
        if svc.get("infra"):
            item(svc["infra"])["evidence"].append(f"compose service '{svc['name']}' ({svc['image']})")
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
        if len(provs) == 1:
            it["label"] = f"{provs[0]} API" if provs[0] != "LiteLLM" else "LLM gateway (LiteLLM)"
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
