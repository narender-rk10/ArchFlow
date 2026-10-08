"""Proposed high-level design (HLD) reconstructed from a normalized code graph and its detections.

Deterministic steps:

1. **Units**: every directory holding a package manifest (pyproject, package.json, go.mod, pom/gradle, Cargo)
   owns the code files below it (deepest manifest wins); code outside any manifest is grouped by top-level
   directory. A manifest-less unit whose code splits into several substantial sub-packages is split into them.
   Tests, fixtures and examples are ignored.
2. **Roles** — each unit is one of:

   * ``service`` (runtime): a server framework, a server entry point, a Dockerfile or a compose service;
   * ``client`` (runtime): a UI framework without a server framework, with an app entry point (``createRoot``,
     an npm ``dev``/``start`` script, a Dockerfile or a compose service);
   * ``library``: anything else (shared packages, UI kits, generated SDKs) — **folded** into the runtime unit(s)
     that import it (its files and ``code_paths`` are appended); never a component of its own;
   * ``tooling``: build-time or project tooling — email templates (react-email, mjml), docs sites, Storybook,
     project scaffolding hooks (copier/cookiecutter ``hooks/``) and ``scripts/`` — never a component.

3. **Compose** (all files merged by service name, see ``detect.merge_compose``): a service whose ``build``
   context or Dockerfile is in a unit's directory, or whose name matches it (``backend`` ↔ ``backend/``,
   ``web`` ↔ ``frontend/``), *is* that unit (``props.compose_service``). Known images are typed
   (``detect.IMAGE_TYPES``): datastores merge with the same infrastructure detected from client libraries
   (compose ``db: postgres`` + psycopg = one PostgreSQL), proxies become gateways (Traefik, NGINX, ...), identity
   providers security, Prometheus/Grafana observability services. Dev/test tools (Adminer, pgAdmin, Mailpit,
   MailHog, Playwright, Selenium, ...) are left out and listed in ``dev_tools`` unless ``include_dev_tools``.
4. **Infrastructure** from client libraries imported in code, compose/k8s images and Terraform: one component per
   kind, named after the technology (PostgreSQL, Redis, ...), matched to the catalog.
5. **Connections** (importer → dependency, runtime units only):

   * unit → infrastructure where the unit's files import the client library;
   * compose ``depends_on`` and service hostnames referenced in compose environment values
     (``DATABASE_URL=...@db``);
   * gateway → the services it routes to (Traefik ``traefik.http.routers.*`` labels; nginx/Caddy/Traefik
     config files);
   * client → web service over HTTP/WebSocket — through the gateway when a gateway routes to that service
     (client → gateway → service). A generated OpenAPI client in the client is recorded as the evidence;
   * unit → unit for code references (imports/calls) between units in the same language, never from tests or
     generated code, and never service → client.

Components carry ``props.code_paths`` (comma-separated top paths), ``props.symbols``, ``props.files`` and the
repository/commit they came from, for traceability. An optional LLM pass may rename runtime services to short
Title Case names (validated, else the deterministic name stays) and rewrite descriptions; it never adds or
removes components and falls back silently.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from pathlib import PurePosixPath
from typing import Any

from .. import catalog, llm
from ..models import ProjectState
from ..ops import apply_ops, slugify
from . import detect
from .detect import INFRA, image_infra, is_codegen_path, is_test_path, language_of

log = logging.getLogger("archflow.codegraph")

CODE_EDGE_RELATIONS = {"calls", "indirect_call", "imports", "imports_from", "uses", "references", "inherits",
                       "extends", "implements", "re_exports", "dynamic_import"}
SKIP_DIRS = {"docs", "doc", "examples", "example", "scripts", "tools", "benchmarks", "bench", "samples", "migrations",
             ".github", "hack", "ci"}
GENERIC_NAMES = {"src", "app", "lib", "pkg", "packages", "apps", "services", "source", "code", ""}
FRAMEWORK_PROTOCOL = {"Hocuspocus": "WebSocket"}
WS_ONLY_FRAMEWORKS = {"Hocuspocus"}
FRAMEWORK_IMPLIES = {"FastAPI": {"Starlette"}, "NestJS": {"Express"}}
INFRA_PROTOCOL = {"postgresql": "SQL", "mysql": "SQL", "sqlite": "SQL (embedded)", "mongodb": "MongoDB",
                  "elasticsearch": "HTTP", "vector": "HTTP", "redis": "Redis", "memcached": "Memcached",
                  "kafka": "Kafka", "rabbitmq": "AMQP", "nats": "NATS", "sqs": "HTTPS", "celery": "AMQP/Redis",
                  "s3": "S3 API", "llm": "HTTPS", "mcp": "MCP"}
RUNTIME_ROLES = {"service", "client"}

# ---- role rules
TOOLING_TOP_DIRS = {"hooks", "scripts", "tools", "bin", "hack", "ci", ".github", "devtools", "tasks", "template",
                    "templates", "cookiecutter", "{{cookiecutter.project_slug}}"}
_EMAIL_DIR = re.compile(r"(^|[/\-_])(e-?mails?|email-templates?|mail-templates?|mjml)([/\-_]|$)", re.I)
_DOCS_DIR = re.compile(r"(^|/)(docs?|documentation|website|storybook|\.storybook|stories)(/|$)", re.I)
EMAIL_DEPS = {"react-email", "@react-email/components", "@react-email/render", "mjml", "@maizzle/framework", "maizzle",
              "jsx-email"}
DOCS_DEPS = {"@docusaurus/core", "vitepress", "nextra", "mkdocs", "mkdocs-material", "sphinx", "@astrojs/starlight"}
CLIENT_DEPS = {"react", "vue", "@angular/core", "svelte", "next", "react-native", "solid-js", "preact"}
SERVER_ENTRY_KINDS = {"web-app", "server", "go-main", "jvm-main", "console-script", "npm-start"}

# ---- names
CLIENT_DIR_NAMES = {"frontend", "front", "web", "webapp", "web-app", "client", "ui", "www", "site", "spa", "app"}
BACKEND_DIR_NAMES = {"backend", "back", "api", "server"}
COMPOSE_SYNONYMS = {"backend": BACKEND_DIR_NAMES, "api": BACKEND_DIR_NAMES, "server": BACKEND_DIR_NAMES,
                    "frontend": CLIENT_DIR_NAMES, "web": CLIENT_DIR_NAMES, "client": CLIENT_DIR_NAMES,
                    "ui": CLIENT_DIR_NAMES, "webapp": CLIENT_DIR_NAMES}
_NAME_SUFFIXES = {"service", "svc", "app", "api", "server", "srv"}
ACRONYMS = {"api": "API", "ui": "UI", "mcp": "MCP", "llm": "LLM", "ai": "AI", "db": "DB", "sql": "SQL", "cms": "CMS",
            "bff": "BFF", "grpc": "gRPC", "ws": "WS", "id": "ID", "iam": "IAM", "sso": "SSO", "etl": "ETL"}


def _pretty(name: str) -> str:
    name = re.sub(r"[_\-./]+", " ", name).strip()
    words = []
    for w in name.split():
        words.append(ACRONYMS.get(w.lower()) or (w if w.isupper() else w[:1].upper() + w[1:]))
    return " ".join(words) or "App"


def _under(path: str, d: str) -> bool:
    return not d or path == d or path.startswith(d + "/")


def _child(path: str, d: str) -> str | None:
    """The first directory below ``d`` that contains ``path`` (None for files directly in ``d``)."""
    rest = path[len(d) + 1:] if d else path
    parts = rest.split("/")
    return (f"{d}/{parts[0]}" if d else parts[0]) if len(parts) > 1 else None


def _lang_family(path: str) -> str:
    lang = language_of(path) or ""
    return {"TypeScript": "js", "JavaScript": "js", "Vue": "js", "Svelte": "js", "Kotlin": "jvm", "Java": "jvm",
            "Scala": "jvm", "Groovy": "jvm", "C": "c", "C++": "c", "Objective-C": "c"}.get(lang, lang)


# --------------------------------------------------------------------------- units
def _units(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    det = graph.get("detections") or {}
    files = [f for f in graph.get("files", []) if not is_test_path(f["path"])
             and not any(p in SKIP_DIRS for p in f["path"].split("/")[:-1])]
    sym_count = Counter(s["file"] for s in graph.get("symbols", []) if s.get("kind") != "file")
    manifests = [m for m in det.get("manifests", []) if not is_test_path(m.get("path", "") + "/")
                 and not any(p in SKIP_DIRS for p in m.get("dir", "").split("/") if p)]
    mdirs = sorted({m.get("dir", "") for m in manifests}, key=lambda d: -len(d))
    mby_dir: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in manifests:
        mby_dir[m.get("dir", "")].append(m)

    groups: dict[str, list[str]] = defaultdict(list)
    for f in files:
        p = f["path"]
        owner = next((d for d in mdirs if _under(p, d)), None)
        if owner is None:
            owner = p.split("/")[0] if "/" in p else ""
        groups[owner].append(p)

    def weight(paths: list[str]) -> int:
        return sum(sym_count.get(p, 0) + 1 for p in paths)

    # Split a unit whose code lives in several substantial sub-packages.
    final: dict[str, list[str]] = {}
    for d, paths in groups.items():
        total = weight(paths)
        base = d
        while True:  # descend through single-child wrappers (src/, the package dir, ...)
            kids: dict[str, list[str]] = defaultdict(list)
            for p in paths:
                kids[_child(p, base) or ""].append(p)
            big = {k: v for k, v in kids.items() if k and weight(v) >= 0.15 * total and weight(v) >= 10}
            if len(big) == 1 and weight(next(iter(big.values()))) >= 0.9 * total and len(paths) > 1:
                base = next(iter(big))
                continue
            break
        # Only the repository root or manifest-less directories are split: a manifest marks one deployable.
        if len(big) >= 2 and (d == "" or not mby_dir.get(d)):
            rest = [p for k, v in kids.items() if k not in big for p in v]
            for k, v in list(big.items())[:8]:
                final[k] = v
            if rest:
                biggest = max(big, key=lambda k: weight(big[k]))
                final[biggest] = final[biggest] + rest
        else:
            final[d] = paths

    units: dict[str, dict[str, Any]] = {}
    for d, paths in final.items():
        if not paths:
            continue
        langs = Counter(language_of(p) or "" for p in paths)
        manifest = next((m for m in mby_dir.get(d, []) if m.get("kind") == "node"), None) \
            or (mby_dir.get(d) or [None])[0]
        deps: set[str] = set()
        for m in mby_dir.get(d, []):
            deps.update(str(x).lower() for x in m.get("dependencies") or [])
        units[d] = {"dir": d, "dirs": [d], "files": sorted(paths), "language": langs.most_common(1)[0][0],
                    "symbols": sum(sym_count.get(p, 0) for p in paths), "manifest": manifest,
                    "manifests": mby_dir.get(d, []), "deps": deps, "compose": [], "frameworks": [], "ui": [],
                    "entry_points": [], "dockerfile": False, "folded": [], "also_paths": []}
    # Units with only a handful of files and no manifest are noise (stray scripts at the top level).
    for d in [d for d, u in units.items() if not u["manifest"] and u["symbols"] < 3 and len(units) > 1]:
        del units[d]
    return units


def unit_of_path(units: dict[str, dict[str, Any]], path: str) -> str | None:
    """The unit owning ``path``: by file membership, else the unit with the deepest matching directory."""
    best, best_dir = None, None
    for d, u in units.items():
        if path in u["_fileset"]:
            return d
        for ud in u.get("dirs", [d]):
            if _under(path, ud) and (best_dir is None or len(ud) > len(best_dir)):
                best, best_dir = d, ud
    return best


def _role(d: str, u: dict[str, Any]) -> tuple[str, str]:
    """(role, reason) — see the module docstring."""
    parts = [p for p in d.split("/") if p]
    top = parts[0].lower() if parts else ""
    names = {str(m.get("name") or "").lower() for m in u["manifests"]}
    deps = u["deps"]
    files = u["files"]
    if top in TOOLING_TOP_DIRS or (files and all(p.split("/")[0].lower() in TOOLING_TOP_DIRS for p in files if "/" in p)
                                   and all("/" in p for p in files)):
        return "tooling", "project tooling (scaffolding hooks / scripts)"
    deployed = bool(u["frameworks"] or u["compose"] or u["dockerfile"])  # a running mail/docs *service* stays
    if not deployed and (_EMAIL_DIR.search(d) or deps & EMAIL_DEPS or any(_EMAIL_DIR.search(n) for n in names if n)):
        return "tooling", "email templates (build-time)"
    if not deployed and (_DOCS_DIR.search(d) or deps & DOCS_DEPS):
        return "tooling", "documentation / Storybook site"
    is_ui = bool(u["ui"]) or bool(deps & CLIENT_DEPS)
    scripts = {}
    for m in u["manifests"]:
        scripts.update(m.get("scripts") or {})
    kinds = {e.get("kind") for e in u["entry_points"]}
    if u["frameworks"]:
        return "service", "server framework " + ", ".join(u["frameworks"][:2])
    if is_ui:
        if u["compose"] or u["dockerfile"] or "web-client" in kinds or {"dev", "start", "serve"} & set(scripts):
            return "client", "UI application"
        return "library", "UI component library"
    if u["compose"]:
        return "service", "compose service " + u["compose"][0]["name"]
    if u["dockerfile"]:
        return "service", "has a Dockerfile"
    if kinds & SERVER_ENTRY_KINDS or (kinds & {"python-main"} and not u["manifest"]):
        return "service", "entry point " + next(iter(kinds & (SERVER_ENTRY_KINDS | {"python-main"})))
    return "library", "no server, UI app or deployment found"


def _unit_by_name(units: dict[str, dict[str, Any]], name: str, roles: set[str] | None = None) -> str | None:
    """Unit whose directory name matches a compose service name (``backend`` ↔ ``backend/``,
    ``orders-api`` ↔ ``services/orders``, ``web`` ↔ ``frontend/``)."""
    n = name.lower()
    toks = [t for t in re.split(r"[-_.]", n) if t]
    stripped = "-".join(t for t in toks if t not in _NAME_SUFFIXES) or n
    candidates = {n, stripped, n.replace("_", "-")}
    found = []
    for d, u in units.items():
        if not d or (roles and u.get("role") not in roles):
            continue
        base = PurePosixPath(d).name.lower()
        if base in candidates:
            found.append((0, d))
        elif base in COMPOSE_SYNONYMS.get(n, set()) or n in COMPOSE_SYNONYMS.get(base, set()):
            found.append((1, d))
    return min(found)[1] if found else None


def _unit_for_build(units: dict[str, dict[str, Any]], svc: dict[str, Any]) -> str | None:
    """Unit built by a compose service: its Dockerfile's directory, else its build context, else its name."""
    dockerfile = svc.get("dockerfile")
    places = []
    if dockerfile:
        parent = str(PurePosixPath(dockerfile).parent)
        places.append("" if parent == "." else parent)
    if svc.get("build") is not None:
        places.append(svc["build"])
    for p in places:
        if p:
            owners = [d for d in units if d and _under(p, d)]
            if owners:
                return max(owners, key=len)
            inside = [d for d in units if d and _under(d, p)]
            if len(inside) == 1:
                return inside[0]
    by_name = _unit_by_name(units, svc["name"])
    if by_name:
        return by_name
    if "" in places and "" in units:
        return ""
    return None


# --------------------------------------------------------------------------- names
def _unit_name(d: str, u: dict[str, Any], root_name: str) -> str:
    base = PurePosixPath(d).name if d else ""
    if base.lower() in GENERIC_NAMES:
        mname = str((u["manifest"] or {}).get("name") or "").split("/")[-1]
        base = mname if mname and mname.lower() not in GENERIC_NAMES else \
            (PurePosixPath(d).parent.name if d and PurePosixPath(d).parent.name else root_name or "app")
    low = base.lower()
    if u["role"] == "client":
        kind = "Mobile App" if "React Native" in u["ui"] else "Web App"
        if low in CLIENT_DIR_NAMES or low in GENERIC_NAMES:
            return kind
        pretty = _pretty(base)
        return pretty if pretty.split()[-1] in ("App", "Client", "Portal", "Dashboard") else f"{pretty} App"
    is_api = bool(u["frameworks"]) and not set(u["frameworks"]) <= WS_ONLY_FRAMEWORKS
    if low in BACKEND_DIR_NAMES:
        return "Backend API" if is_api else "Backend Service"
    words = _pretty(base).split()
    if words[-1].lower() in _NAME_SUFFIXES | {"worker", "gateway"}:
        return " ".join(words)
    return " ".join(words + ["API" if is_api else "Service"])


# --------------------------------------------------------------------------- reconstruction
def reconstruct(graph: dict[str, Any], *, repo_url: str | None = None, commit_sha: str | None = None,
                code_graph_id: str | None = None, include_dev_tools: bool = False) -> dict[str, Any]:
    """Return {"nodes": [...], "edges": [...], "dev_tools": [...], "excluded": [...]} — node dicts are ready for
    ``add_node`` ops (with ids); ``dev_tools`` are compose dev/test tools left out of the architecture (included
    as components with ``include_dev_tools``); ``excluded`` are code units that are not runtime components."""
    det = graph.get("detections") or {}
    root_name = graph.get("root_name") or "app"
    units = _units(graph)
    for u in units.values():
        u["_fileset"] = set(u["files"])
    for fw in det.get("frameworks", []):
        for p in fw.get("files", []):
            d = unit_of_path(units, p)
            if d is None:
                continue
            target = units[d]["frameworks" if fw.get("kind") == "web-framework" else "ui"]
            if fw["name"] not in target:
                target.append(fw["name"])
    for ep in det.get("entry_points", []):
        d = unit_of_path(units, ep.get("path", ""))
        if d is not None:
            units[d]["entry_points"].append(ep)
    for df in det.get("dockerfiles", []):
        name = PurePosixPath(df.get("path", "")).name.lower()
        if re.search(r"playwright|cypress|selenium|e2e|test|dev", name):
            continue
        d = df.get("dir", "")
        if d in units:
            units[d]["dockerfile"] = True

    # ---- compose: dev tools out, build/name → unit, the rest typed by image
    compose = detect.merge_compose(det.get("compose_services", []))
    dev_svcs = [s for s in compose if s.get("dev_tool")]
    rt_svcs = [s for s in compose if not s.get("dev_tool")]
    svc_unit: dict[str, str] = {}
    for svc in rt_svcs:
        typ = detect.image_type(svc.get("image", "")) if svc.get("build") is None else None
        if svc.get("build") is not None:
            d = _unit_for_build(units, svc)
        elif typ is None:
            d = _unit_by_name(units, svc["name"])
        else:
            d = None
        if d is not None:
            svc_unit[svc["name"]] = d
            units[d]["compose"].append(svc)

    for d, u in units.items():
        u["role"], u["reason"] = _role(d, u)
    if units and not any(u["role"] in RUNTIME_ROLES for u in units.values()):
        # a pure library repository: its biggest package is the component
        libs = [d for d, u in units.items() if u["role"] == "library"]
        if libs:
            d = max(libs, key=lambda k: units[k]["symbols"])
            units[d]["role"], units[d]["reason"] = "service", "main package of a library repository"

    # ---- code references between units (tests, generated code and cross-language matches ignored)
    file_unit: dict[str, str | None] = {}
    sym_file: dict[str, str] = {}
    for s in graph.get("symbols", []):
        sym_file[s["id"]] = s["file"]
        if s["file"] not in file_unit:
            file_unit[s["file"]] = unit_of_path(units, s["file"])
    raw_cross: Counter[tuple[str, str]] = Counter()
    for e in graph.get("edges", []):
        if e.get("relation") not in CODE_EDGE_RELATIONS:
            continue
        fa, fb = sym_file.get(e["source"]), sym_file.get(e["target"])
        if not fa or not fb or is_test_path(fa) or is_test_path(fb) or is_codegen_path(fa) or is_codegen_path(fb):
            continue
        if _lang_family(fa) != _lang_family(fb):
            continue
        a, b = file_unit.get(fa), file_unit.get(fb)
        if a is not None and b is not None and a != b:
            raw_cross[(a, b)] += 1

    # ---- fold libraries into the runtime units that import them; drop tooling
    excluded: list[dict[str, Any]] = []
    manifest_names = {d: {str(m.get("name") or "").lower() for m in u["manifests"] if m.get("name")}
                      for d, u in units.items()}
    for d in sorted([d for d, u in units.items() if u["role"] in ("library", "tooling")], key=len, reverse=True):
        u = units[d]
        if u["role"] == "tooling":
            excluded.append({"path": d or ".", "role": "tooling", "reason": u["reason"], "files": len(u["files"])})
            del units[d]
            continue
        refs: Counter[str] = Counter()
        for (a, b), n in raw_cross.items():
            if b == d and a in units and a != d:
                refs[a] += n
        for other, ou in units.items():
            if other != d and manifest_names.get(d, set()) & ou["deps"]:
                refs[other] += 1
        runtime_refs = [k for k, _ in refs.most_common() if units[k]["role"] in RUNTIME_ROLES]
        if not runtime_refs:  # nested inside a runtime unit's directory?
            runtime_refs = sorted([k for k, ku in units.items() if k != d and ku["role"] in RUNTIME_ROLES
                                   and _under(d, k)], key=len, reverse=True)[:1]
        if not runtime_refs:
            excluded.append({"path": d or ".", "role": "library", "reason": "not imported by any runtime unit",
                             "files": len(u["files"])})
            del units[d]
            continue
        primary = units[runtime_refs[0]]
        primary["files"] = sorted(set(primary["files"]) | set(u["files"]))
        primary["_fileset"] |= u["_fileset"]
        primary["symbols"] += u["symbols"]
        primary["dirs"] += [x for x in u["dirs"] if x]  # a root package is matched by its files only
        primary["folded"].append(d or ".")
        for k in runtime_refs[1:]:
            units[k]["also_paths"].append(d or ".")
        for (a, b), n in list(raw_cross.items()):  # re-attribute the library's own references
            if a == d or b == d:
                del raw_cross[(a, b)]
                a2, b2 = (runtime_refs[0] if a == d else a), (runtime_refs[0] if b == d else b)
                if a2 != b2:
                    raw_cross[(a2, b2)] += n
        del units[d]

    common = {"source": "code"}
    if repo_url:
        common["code_repo"] = repo_url
    if commit_sha:
        common["code_commit"] = commit_sha
    if code_graph_id:
        common["code_graph"] = code_graph_id

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    used_names: set[str] = set()

    def new_id(name: str) -> str:
        base = slugify(name)
        nid, i = base, 2
        while nid in used_ids:
            nid, i = f"{base}-{i}", i + 1
        used_ids.add(nid)
        return nid

    def unique_name(name: str, d: str) -> str:
        if name not in used_names:
            used_names.add(name)
            return name
        alt = _pretty(" ".join(p for p in d.split("/") if p.lower() not in GENERIC_NAMES)) + " " + name.split()[-1]
        name = alt if alt not in used_names else f"{name} {len(used_names) + 1}"
        used_names.add(name)
        return name

    # ---- runtime units → services / clients
    unit_node: dict[str, str] = {}
    for d, u in sorted(units.items(), key=lambda kv: (kv[1]["role"] != "service", -kv[1]["symbols"], kv[0])):
        is_client = u["role"] == "client"
        name = unique_name(_unit_name(d, u, root_name), d)
        fws = [f for f in u["frameworks"] if not any(f in FRAMEWORK_IMPLIES.get(g, ()) for g in u["frameworks"])]
        tech_parts = (u["ui"] if is_client else fws)[:2] + ([u["language"]] if u["language"] else [])
        if is_client and not u["ui"]:
            tech_parts = [x for x in ("React" if "react" in u["deps"] else "Vue" if "vue" in u["deps"] else "",)
                          if x] + tech_parts
        technology = " · ".join(dict.fromkeys(tech_parts))
        entry = next((e["path"] for e in u["entry_points"] if e.get("kind") in SERVER_ENTRY_KINDS | {"web-client"}),
                     u["entry_points"][0]["path"] if u["entry_points"] else "")
        kind = "client" if is_client else "service"
        role = "Front-end application" if is_client else "Runtime service"
        desc = f"{role} reconstructed from {len(u['files'])} source file(s) in {d or 'the repository root'}"
        desc += f" ({u['symbols']} symbols)." + (f" Entry point: {entry}." if entry else "")
        if u["folded"]:
            desc += " Includes the shared package(s) " + ", ".join(u["folded"]) + "."
        top = d or ", ".join(sorted({p.split("/")[0] for p in u["files"] if "/" in p})[:5]) or "."
        code_paths = ", ".join(dict.fromkeys([top] + u["folded"] + u["also_paths"]))
        cat_entry = catalog.match({"technology": technology, "name": ""}) if is_client else None
        ok_cat = bool(cat_entry and cat_entry["kind"].startswith("client."))
        props = {**common, "code_paths": code_paths, "symbols": str(u["symbols"]), "files": str(len(u["files"])),
                 "role": u["role"]}
        if entry:
            props["entry_point"] = entry
        if u["compose"]:
            props["compose_service"] = ", ".join(s["name"] for s in u["compose"])
        nid = new_id(name)
        unit_node[d] = nid
        nodes.append({"id": nid, "name": name, "type": kind, "technology": technology, "description": desc,
                      "catalog_id": cat_entry["id"] if ok_cat else "", "vendor": cat_entry["vendor"] if ok_cat else "",
                      "props": props, "_unit": d, "_dirs": list(u["dirs"]), "_frameworks": u["frameworks"],
                      "_client": is_client, "_openapi": _has_openapi_client(u)})

    # ---- infrastructure: library detections merged with typed compose datastores
    infra_node: dict[str, str] = {}
    infra_items = {i.get("key"): dict(i) for i in det.get("infrastructure", []) if i.get("key") in INFRA}
    compose_infra: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for svc in rt_svcs:
        if svc["name"] in svc_unit:
            continue
        typ = detect.image_type(svc.get("image", ""))
        key = (typ or {}).get("infra") or svc.get("infra")
        if key and key in INFRA:
            compose_infra[key].append({**svc, "_type": typ})
    for key, svcs in compose_infra.items():
        if key not in infra_items:
            label, category, ntype, cid, tech = INFRA[key]
            infra_items[key] = {"key": key, "label": label, "category": category, "type": ntype, "catalog_id": cid,
                                "technology": tech, "files": [], "evidence": []}
        it = infra_items[key]
        typ = svcs[0].get("_type") or {}
        if typ.get("name") and (not it.get("files") or key in ("s3", "vector") or typ["name"] != INFRA[key][0]):
            it["label"] = typ["name"]  # the image is more specific (MinIO, Valkey, MariaDB, Qdrant, ...)
            if typ["name"] not in ("Redis", "PostgreSQL", "MySQL"):
                it["technology"] = typ["name"]
            if typ.get("catalog_id"):
                it["catalog_id"] = typ["catalog_id"]
        for s in svcs:
            ev = f"compose service '{s['name']}' ({s.get('image', '')})"
            if ev not in (it.get("evidence") or []):
                it["evidence"] = list(it.get("evidence") or []) + [ev]
    order = {c: i for i, c in enumerate(detect.INFRA_CATEGORY_ORDER)}
    for key, item in sorted(infra_items.items(), key=lambda kv: (order.get(kv[1].get("category"), 9), kv[0])):
        files = [p for p in item.get("files", []) if not is_test_path(p)]
        evidence = [e for e in item.get("evidence") or [] if not any(
            f"'{s['name']}'" in e for s in dev_svcs)]
        if not files and not evidence:
            continue
        cid = item.get("catalog_id") or ""
        entry = catalog.BY_ID.get(cid)
        paths = sorted({unit_of_path(units, p) or p.split("/")[0] for p in files})
        name = unique_name(item.get("label") or INFRA[key][0], key)
        nid = new_id(name)
        infra_node[key] = nid
        in_compose = [s["name"] for s in compose_infra.get(key, [])]
        desc = (f"Detected from {len(files)} file(s) importing its client library" if files
                else "Detected from deployment files")
        if evidence:
            desc += f"; {evidence[0]}"
        props = {**common, "code_paths": ", ".join(p for p in paths if p)[:300] or "", "files": str(len(files)),
                 "detected_from": key,
                 "evidence": "code+compose" if files and in_compose else "code" if files else "deployment"}
        if in_compose:
            props["compose_service"] = ", ".join(in_compose)
        nodes.append({"id": nid, "name": name, "type": item.get("type") or INFRA[key][2],
                      "technology": item.get("technology") or (entry["name"] if entry else ""),
                      "description": desc + ".", "catalog_id": cid if entry else "",
                      "vendor": entry["vendor"] if entry else "", "props": props, "_infra": key})
        users = Counter(unit_of_path(units, p) for p in files)
        for d, n in users.items():
            if d is None or d not in unit_node:
                continue
            src, tgt = unit_node[d], nid
            if key == "mcp":
                src, tgt = nid, unit_node[d]
            edges.append({"source": src, "target": tgt, "protocol": INFRA_PROTOCOL.get(key, ""),
                          "label": f"{n} file(s) use it"})

    # ---- other compose containers: gateways, identity, observability, unknown images
    by_compose: dict[str, str] = {name: unit_node[d] for name, d in svc_unit.items() if d in unit_node}
    for key, svcs in compose_infra.items():
        if key in infra_node:
            for s in svcs:
                by_compose[s["name"]] = infra_node[key]
    platform: dict[str, dict[str, Any]] = {}  # node id → image type

    def container_node(svc: dict[str, Any], dev: bool = False) -> str:
        typ = detect.image_type(svc.get("image", "")) or {}
        img_words = detect.image_words(svc.get("image", ""))
        tech = typ.get("name") or (img_words[-1] if img_words and svc.get("image") else "") or "Container"
        name = unique_name(typ.get("name") or _pretty(svc["name"]), svc["name"])
        nid = new_id(name)
        entry = catalog.BY_ID.get(typ.get("catalog_id", ""))
        props = {**common, "code_paths": "", "compose_service": svc["name"],
                 "compose_files": ", ".join(svc.get("files") or [])}
        if dev:
            props["dev_tool"] = "true"
        nodes.append({"id": nid, "name": name, "type": typ.get("type", "service"), "technology": tech,
                      "description": (f"Dev/test tool container '{svc['name']}'" if dev else
                                      f"Container '{svc['name']}'") + f" from {', '.join(svc.get('files') or [])}.",
                      "catalog_id": entry["id"] if entry else "", "vendor": entry["vendor"] if entry else "",
                      "props": props, "_compose": svc["name"]})
        by_compose[svc["name"]] = nid
        platform[nid] = {**typ, "image_words": img_words}
        return nid

    for svc in rt_svcs:
        if svc["name"] not in by_compose and not (svc.get("infra") or image_infra(svc.get("image", ""))):
            container_node(svc)
    if include_dev_tools:
        for svc in dev_svcs:
            container_node(svc, dev=True)
    compose_by_name = {s["name"]: s for s in (rt_svcs + (dev_svcs if include_dev_tools else []))}

    # ---- compose depends_on and hostnames in environment values
    for svc in compose_by_name.values():
        src = by_compose.get(svc["name"])
        for dep in svc.get("depends_on", []):
            tgt = by_compose.get(dep)
            if src and tgt and src != tgt:
                edges.append({"source": src, "target": tgt, "protocol": _protocol_to(nodes, tgt),
                              "label": "depends_on (compose)"})
        for host in svc.get("env_hosts", []):
            tgt = by_compose.get(host)
            if src and tgt and src != tgt:
                edges.append({"source": src, "target": tgt, "protocol": _protocol_to(nodes, tgt),
                              "label": "configured endpoint (compose environment)"})

    # ---- gateway routing: Traefik labels and proxy config files
    gateways = [n for n in nodes if n["type"] == "gateway" and n.get("_compose")]
    routes: dict[str, set[str]] = defaultdict(set)  # gateway id → routed node ids
    for g in gateways:
        if "Traefik" in (g["technology"], g["name"]):
            for svc in compose_by_name.values():
                tgt = by_compose.get(svc["name"])
                if tgt and tgt != g["id"] and detect.traefik_routed(svc):
                    routes[g["id"]].add(tgt)
    for cfg in det.get("proxy_routes", []):
        kind = cfg.get("kind", "")
        gw = next((g for g in gateways if kind in (g["technology"] + " " + g["name"]).lower()), None)
        src = gw["id"] if gw else None
        if src is None:  # e.g. the nginx inside a front-end container proxying /api
            d = unit_of_path(units, cfg.get("path", ""))
            src = unit_node.get(d) if d is not None and cfg.get("path", "").startswith(d) and d else None
        if not src:
            continue
        for host in cfg.get("targets", []):
            tgt = by_compose.get(host) or unit_node.get(_unit_by_name(units, host) or "\0")
            if tgt and tgt != src:
                if gw:
                    routes[src].add(tgt)
                else:
                    edges.append({"source": src, "target": tgt, "protocol": "HTTP",
                                  "label": f"proxied ({cfg.get('path')})"})
    for gid, targets in routes.items():
        for tgt in sorted(targets):
            edges.append({"source": gid, "target": tgt, "protocol": "HTTP", "label": "routes (gateway config)"})

    # ---- clients → web services (through the gateway that routes to them, if any)
    services = [n for n in nodes if n.get("_unit") is not None and not n.get("_client") and n.get("_frameworks")]
    for c in [n for n in nodes if n.get("_client")]:
        evidence = "generated OpenAPI client" if c.get("_openapi") else "inferred"
        for s in services:
            proto = next((FRAMEWORK_PROTOCOL[f] for f in s["_frameworks"] if f in FRAMEWORK_PROTOCOL), "HTTP")
            via = next((gid for gid, t in routes.items() if s["id"] in t), None)
            if via:
                edges.append({"source": c["id"], "target": via, "protocol": proto, "label": f"{evidence}, via gateway"})
            else:
                edges.append({"source": c["id"], "target": s["id"], "protocol": proto, "label": evidence})

    # ---- code references across runtime units
    for (a, b), n in raw_cross.most_common():
        if a not in unit_node or b not in unit_node:
            continue
        if units[a]["role"] == "service" and units[b]["role"] == "client":
            continue  # a backend never imports a front-end (name collisions, shared generated types)
        edges.append({"source": unit_node[a], "target": unit_node[b], "protocol": "import",
                      "label": f"{n} code reference(s)"})

    # de-duplicate connections (first wins)
    seen, uniq = set(), []
    for e in edges:
        k = (e["source"], e["target"])
        if k not in seen and e["source"] != e["target"]:
            seen.add(k)
            uniq.append(e)
    _layout(nodes)
    dev_tools = [{"name": s["name"], "image": s.get("image", ""), "files": s.get("files", []),
                  "included": include_dev_tools} for s in dev_svcs]
    return {"nodes": nodes, "edges": uniq, "dev_tools": dev_tools, "excluded": excluded}


def _has_openapi_client(u: dict[str, Any]) -> bool:
    return bool(u["deps"] & detect.OPENAPI_CLIENT_DEPS) or any(detect.is_openapi_client_file(p) for p in u["files"])


def _protocol_to(nodes: list[dict[str, Any]], nid: str) -> str:
    n = next((x for x in nodes if x["id"] == nid), None)
    if n and n.get("_infra"):
        return INFRA_PROTOCOL.get(n["_infra"], "")
    if n and n["type"] in ("service", "gateway") and n.get("_unit") is not None:
        return "HTTP" if n.get("_frameworks") else ""
    return ""


LAYER = {"client": 0, "gateway": 1, "security": 1, "mcp": 1, "service": 2, "agent": 2, "external": 2,
         "database": 3, "cache": 3, "queue": 3, "storage": 3, "llm": 3}


def _layout(nodes: list[dict[str, Any]]) -> None:
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        rows[LAYER.get(n["type"], 2)].append(n)
    for layer, items in rows.items():
        width = (len(items) - 1) * 260
        for i, n in enumerate(items):
            n["position"] = {"x": i * 260 - width / 2, "y": layer * 190}


def public(recon: dict[str, Any]) -> dict[str, Any]:
    """Without the private bookkeeping keys (``_unit``, ...)."""
    return {"nodes": [{k: v for k, v in n.items() if not k.startswith("_")} for n in recon["nodes"]],
            "edges": recon["edges"], "dev_tools": recon.get("dev_tools", []), "excluded": recon.get("excluded", [])}


def to_state(recon: dict[str, Any]) -> ProjectState:
    """The reconstruction as an in-memory ProjectState (used by drift and impact)."""
    pub = public(recon)
    ops = [{"op": "add_node", "node": n} for n in pub["nodes"]]
    ops += [{"op": "add_edge", "edge": e} for e in pub["edges"]]
    state, _ = apply_ops(ProjectState(), ops)
    return state


# --------------------------------------------------------------------------- optional LLM refinement
REFINE_SCHEMA = {
    "type": "object",
    "properties": {"components": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"}},
        "required": ["id"]}}},
    "required": ["components"],
}
REFINE_SYSTEM = ("You name and describe software architecture components reconstructed from a code repository. "
                 "For each component return a one-sentence description of what it does. For components of type "
                 "'service' you may also propose a better name: a short Title Case noun phrase of at most 3 words "
                 "naming its business role (e.g. 'Orders API', 'Billing Worker'), with no parentheses, no "
                 "technology or framework names and no lists. Keep the given name if unsure. Do not invent "
                 "components; keep every id exactly as given.")
_TITLE_WORD = re.compile(r"^(?:[A-Z][a-z0-9]*|[A-Z0-9]{2,5}|[A-Z][a-z]+[0-9]*)$")
_TECH_WORDS = {"react", "typescript", "javascript", "python", "fastapi", "starlette", "django", "flask", "express",
               "node", "nodejs", "vue", "angular", "svelte", "java", "spring", "go", "golang", "rust", "postgresql",
               "postgres", "redis", "docker", "kubernetes", "nginx", "traefik", "hocuspocus", "vite", "next", "nextjs"}


def valid_name(name: str, technology: str = "") -> bool:
    """A proposed component name: 1–3 Title Case words (acronyms allowed), ≤ 40 characters, no parentheses,
    punctuation lists or technology names."""
    name = name.strip()
    if not name or len(name) > 40 or re.search(r"[()\[\],;:/·&+|]", name):
        return False
    words = name.split()
    if not 1 <= len(words) <= 3 or not all(_TITLE_WORD.match(w) for w in words):
        return False
    tech = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", technology)} | _TECH_WORDS
    return not any(w.lower() in tech for w in words)


def refine(recon: dict[str, Any], graph: dict[str, Any], org_id: str | None = None) -> str:
    """Improve names/descriptions with the LLM when available. Returns the engine used ("llm" or "offline").
    Only runtime services (code units of type service) can be renamed, and only to a name ``valid_name``
    accepts; any other proposal keeps the deterministic name."""
    if not llm.available(org_id):
        return "offline"
    comms = sorted(graph.get("communities", []), key=lambda c: -c.get("size", 0))[:15]
    lines = [f"- id={n['id']} type={n['type']} name={n['name']!r} technology={n.get('technology', '')!r} "
             f"paths={n['props'].get('code_paths', '')!r}" for n in recon["nodes"]]
    prompt = ("Repository: " + (graph.get("root_name") or "") + "\nComponents:\n" + "\n".join(lines) +
              "\nLargest code communities (hub symbol @ paths): " +
              "; ".join(f"{c['label']} @ {', '.join(c.get('paths', [])[:2])}" for c in comms))
    try:
        data = llm.complete_json(REFINE_SYSTEM, [{"role": "user", "content": prompt[:12000]}], REFINE_SCHEMA,
                                 group="fast",
                                 org_id=org_id, max_tokens=2000)
    except Exception as exc:  # silent fallback: refinement is cosmetic
        log.info("reconstruction refinement skipped: %s", exc)
        return "offline"
    by_id = {n["id"]: n for n in recon["nodes"]}
    taken = {n["name"] for n in recon["nodes"]}
    for c in data.get("components", []):
        n = by_id.get(str(c.get("id")))
        if not n:
            continue
        name = str(c.get("name") or "").strip()
        if (name and name != n["name"] and n["type"] == "service" and n.get("_unit") is not None
                and valid_name(name, n.get("technology", "")) and name not in taken):
            taken.discard(n["name"])
            n["name"] = name
            taken.add(name)
        desc = str(c.get("description") or "").strip()
        if desc:
            n["description"] = desc[:400]
    return "llm"
