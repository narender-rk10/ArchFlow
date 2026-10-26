"""Official cloud documentation as context for the AI, from the providers' MCP servers.

When a request is about Azure or AWS, the agents get the top matching official documents (reference
architectures, service guides, Well-Architected guidance) as a ``<cloud_docs>`` block in their prompt, so service
choices, limits and best practices follow current documentation rather than the model's memory.

Sources (Streamable HTTP, no authentication; each can be replaced or turned off):
    azure  Microsoft Learn   ARCHFLOW_DOCS_AZURE  default https://learn.microsoft.com/api/mcp
    aws    AWS Knowledge     ARCHFLOW_DOCS_AWS    default https://knowledge-mcp.global.api.aws
A value of ``off`` turns one source off; ``stdio:<command>`` runs a local MCP server instead, e.g. the AWS
Documentation server: ``ARCHFLOW_DOCS_AWS=stdio:uvx awslabs.aws-documentation-mcp-server@latest``.
``ARCHFLOW_DOCS=off`` turns the lookups off altogether (they are also off when ``ARCHFLOW_AI=offline``).

Only the search query leaves ArchFlow (a short phrase built from the request and the technologies on the
canvas). Results are cached per query; a failing source is skipped for a minute and never fails a feature.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("archflow.cloud_docs")

TIMEOUT_S = 12.0
RESULTS = 4                 # documents per source
DOC_CHARS = 1400            # per document
BLOCK_CHARS = 9000          # whole block
CACHE_TTL_S = 6 * 3600
CACHE_SIZE = 256
COOLDOWN_S = 60.0


@dataclass(frozen=True)
class Source:
    cloud: str
    name: str                       # prefixed to queries that do not name the cloud
    label: str
    env: str
    default: str


SOURCES = {
    "azure": Source("azure", "Azure", "Microsoft Learn", "ARCHFLOW_DOCS_AZURE", "https://learn.microsoft.com/api/mcp"),
    "aws": Source("aws", "AWS", "AWS Knowledge", "ARCHFLOW_DOCS_AWS", "https://knowledge-mcp.global.api.aws"),
}

# Words that tie a request to one cloud. Generic terms shared by several clouds (Kubernetes, Redis, …) are left out.
_PATTERNS = {
    "azure": re.compile(r"\b(azure|microsoft entra|entra id|cosmos ?db|aks|app service|service bus|event hubs?|"
                        r"event grid|azure functions|logic apps|front door|blob storage|azure sql|synapse|"
                        r"microsoft fabric|key vault|api management|container apps|ai foundry|bicep)\b", re.I),
    "aws": re.compile(r"\b(aws|amazon|lambda|dynamodb|s3|ec2|ecs|eks|fargate|sqs|sns|eventbridge|kinesis|"
                      r"cloudfront|route ?53|api gateway|aurora|rds|cognito|bedrock|step functions|"
                      r"cloudformation|cdk|elasticache|redshift|sagemaker|opensearch service)\b", re.I),
}


@dataclass(frozen=True)
class DocsRequest:
    """What to look up: ``query`` is sent to the sources; ``about`` (never sent) decides which clouds apply."""
    query: str
    about: str = ""


def enabled() -> bool:
    if os.environ.get("ARCHFLOW_DOCS", "").strip().lower() in {"off", "0", "false", "no"}:
        return False
    return os.environ.get("ARCHFLOW_AI", "auto").strip().lower() != "offline"


def server(cloud: str) -> str:
    """The configured server of a source ('' when it is off)."""
    src = SOURCES[cloud]
    value = os.environ.get(src.env)
    value = src.default if value is None else value.strip()
    return "" if value.lower() in {"", "off", "0", "false", "no"} else value


def clouds(text: str) -> list[str]:
    """The clouds a request is about, most mentioned first."""
    counts = {c: len(p.findall(text or "")) for c, p in _PATTERNS.items()}
    return [c for c, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n]


# --------------------------------------------------------------------------- MCP calls
def _connect(target: str) -> Any:
    from mcp import Client, StdioServerParameters

    if target.startswith("stdio:"):
        cmd = shlex.split(target[len("stdio:"):], posix=os.name != "nt")
        return Client(StdioServerParameters(command=cmd[0], args=cmd[1:]), read_timeout_seconds=TIMEOUT_S)
    return Client(target, read_timeout_seconds=TIMEOUT_S)


def _search_tool(tools: list[Any]) -> Any:
    names = {t.name: t for t in tools}
    for suffix in ("docs_search", "search_documentation"):
        for name, tool in names.items():
            if name.endswith(suffix):
                return tool
    raise LookupError(f"no documentation search tool (has {', '.join(names)})")


def _arguments(tool: Any, query: str, limit: int) -> dict[str, Any]:
    props = (getattr(tool, "input_schema", None) or {}).get("properties") or {}
    key = "search_phrase" if "search_phrase" in props else "query"
    args: dict[str, Any] = {key: query}
    if "limit" in props:
        args["limit"] = limit
    if "topics" in props:  # AWS Knowledge: documentation, not troubleshooting threads or blog round-ups
        args["topics"] = ["reference_documentation"]
    return args


def _records(value: Any) -> list[dict[str, Any]]:
    """The first list of documents in a result, whatever the server wraps it in."""
    if isinstance(value, list):
        if value and all(isinstance(v, dict) for v in value):
            return value
        return []
    if isinstance(value, dict):
        for key in ("results", "result", "content", "documents", "items"):
            if key in value:
                found = _records(value[key])
                if found:
                    return found
    return []


def _parse(result: Any) -> list[dict[str, str]]:
    found = _records(getattr(result, "structured_content", None))
    if not found:
        for part in getattr(result, "content", None) or []:
            text = getattr(part, "text", "")
            try:
                found = _records(json.loads(text))
            except (TypeError, ValueError):
                continue
            if found:
                break
    docs = []
    for r in found:
        url = str(r.get("url") or r.get("contentUrl") or r.get("link") or "")
        body = str(r.get("content") or r.get("context") or r.get("summary") or r.get("snippet") or "")
        title = str(r.get("title") or url)
        if title and body:
            docs.append({"title": " ".join(title.split()), "url": url, "content": body.strip()})
    return docs


async def _search(target: str, query: str, limit: int) -> list[dict[str, str]]:
    async with _connect(target) as client:
        tool = _search_tool((await client.list_tools()).tools)
        result = await client.call_tool(tool.name, _arguments(tool, query, limit))
        if getattr(result, "is_error", False):
            raise RuntimeError(" ".join(getattr(p, "text", "") for p in result.content)[:300])
        return _parse(result)


def _run(coro_fn: Any) -> Any:
    """Run a coroutine on a private loop in a helper thread (callers may already be inside an event loop)."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(asyncio.wait_for(coro_fn(), TIMEOUT_S))
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(TIMEOUT_S + 5)
    if t.is_alive():
        raise TimeoutError(f"no answer within {TIMEOUT_S:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


# --------------------------------------------------------------------------- cache
_cache: OrderedDict[tuple[str, str], tuple[float, list[dict[str, str]]]] = OrderedDict()
_cache_lock = threading.Lock()
_key_locks: dict[tuple[str, str], threading.Lock] = {}
_down_until: dict[str, float] = {}


def clear() -> None:
    with _cache_lock:
        _cache.clear()
        _key_locks.clear()
        _down_until.clear()


def search(cloud: str, query: str, limit: int = RESULTS) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Documents for ``query`` from one cloud's source, and what happened (for the trace). Never raises."""
    target = server(cloud)
    info: dict[str, Any] = {"source": SOURCES[cloud].label, "query": query, "cached": False}
    if not target:
        return [], {**info, "skipped": "source off"}
    if _down_until.get(cloud, 0) > time.monotonic():
        return [], {**info, "skipped": "source failed recently"}
    key = (cloud, " ".join(query.lower().split()))
    with _cache_lock:
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:  # agents fired in parallel with the same query share one call
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
                _cache.move_to_end(key)
                return hit[1], {**info, "cached": True, "results": len(hit[1])}
        t0 = time.monotonic()
        try:
            docs = _run(lambda: _search(target, query, limit))[:limit]
        except BaseException as exc:  # noqa: BLE001 - documentation is optional context
            _down_until[cloud] = time.monotonic() + COOLDOWN_S
            log.warning("%s search failed: %s: %s", SOURCES[cloud].label, type(exc).__name__, exc)
            return [], {**info, "latency_ms": int((time.monotonic() - t0) * 1000),
                        "error": f"{type(exc).__name__}: {exc}"[:300]}
        with _cache_lock:
            _cache[key] = (time.monotonic(), docs)
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
        return docs, {**info, "results": len(docs), "latency_ms": int((time.monotonic() - t0) * 1000)}


# --------------------------------------------------------------------------- prompt block
def _clip(text: str, n: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"


def block(docs_by_cloud: dict[str, list[dict[str, str]]]) -> str:
    """The ``<cloud_docs>`` prompt block ('' when nothing was found)."""
    parts, used, n = [], 0, 0
    seen: set[str] = set()
    for cloud, docs in docs_by_cloud.items():
        for d in docs:
            if d["url"] and d["url"] in seen:
                continue
            seen.add(d["url"])
            n += 1
            entry = f"[{n}] {d['title']} ({SOURCES[cloud].label})\n{d['url']}\n{_clip(d['content'], DOC_CHARS)}"
            if used + len(entry) > BLOCK_CHARS:
                break
            parts.append(entry)
            used += len(entry)
    if not parts:
        return ""
    return "<cloud_docs>\n" + "\n\n".join(parts) + "\n</cloud_docs>"


def query_for(cloud: str, query: str, about: str = "", services: int = 4) -> str:
    """The query sent to one source: the cloud's name and the services the request involves, then the query."""
    pattern = _PATTERNS[cloud]
    named = {m.lower() for m in pattern.findall(query)}
    extra: list[str] = []
    for m in pattern.findall(about):
        if m.lower() not in named and m.lower() not in {e.lower() for e in extra} and m.lower() != cloud:
            extra.append(m)
    prefix = [] if cloud in named else [SOURCES[cloud].name]
    return " ".join(" ".join([*prefix, *extra[:services], query]).split())[:300]


def references(req: DocsRequest | None) -> tuple[str, list[dict[str, Any]]]:
    """(prompt block, one trace record per source searched) for a request; ('', []) when no cloud applies."""
    if req is None or not req.query.strip() or not enabled():
        return "", []
    found: dict[str, list[dict[str, str]]] = {}
    calls: list[dict[str, Any]] = []
    for cloud in clouds(f"{req.about}\n{req.query}"):
        docs, info = search(cloud, query_for(cloud, req.query, req.about))
        calls.append({**info, "titles": [d["title"] for d in docs], "urls": [d["url"] for d in docs]})
        if docs:
            found[cloud] = docs
    return block(found), calls
