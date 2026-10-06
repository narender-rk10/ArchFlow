"""Proposed high-level design (HLD) reconstructed from a normalized code graph and its detections.

Deterministic steps:

1. **Units** (deployable apps/services): every directory holding a package manifest (pyproject, package.json,
   go.mod, pom/gradle, Cargo) owns the code files below it (deepest manifest wins); code outside any manifest is
   grouped by top-level directory. A unit whose code splits into several substantial sub-packages is split into
   them. Tests, fixtures and examples are ignored.
2. **Kind and technology**: UI frameworks (React, Vue, ...) without a server framework → ``client``; otherwise
   ``service``, with the web framework(s) and main language as technology. docker-compose services that build a
   unit's directory give it their name.
3. **Infrastructure** from detections (client libraries imported in code, compose/k8s images, Terraform):
   databases, caches, queues, object storage, LLM providers, MCP — one component per kind, matched to the catalog.
4. **Connections**: unit → infrastructure where the unit's files import the client library (or compose
   ``depends_on``); unit → unit for code references across units (imports/calls) and compose ``depends_on``;
   client → web service over HTTP/WebSocket (inferred).

Components carry ``props.code_paths`` (comma-separated top paths), ``props.symbols``, ``props.files`` and the
repository/commit they came from, for traceability. An optional LLM pass may rename and describe components; it
never adds or removes any and falls back silently.
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
from .detect import INFRA, image_infra, is_test_path, language_of

log = logging.getLogger("archflow.codegraph")

CODE_EDGE_RELATIONS = {"calls", "indirect_call", "imports", "imports_from", "uses", "references", "inherits",
                       "extends", "implements", "re_exports", "dynamic_import"}
SKIP_DIRS = {"docs", "doc", "examples", "example", "scripts", "tools", "benchmarks", "bench", "samples", "migrations",
             ".github", "hack", "ci"}
GENERIC_NAMES = {"src", "app", "lib", "pkg", "packages", "apps", "services", "source", "code", ""}
FRAMEWORK_PROTOCOL = {"Hocuspocus": "WebSocket"}
INFRA_PROTOCOL = {"postgresql": "SQL", "mysql": "SQL", "sqlite": "SQL (embedded)", "mongodb": "MongoDB",
                  "elasticsearch": "HTTP", "vector": "HTTP", "redis": "Redis", "kafka": "Kafka", "rabbitmq": "AMQP",
                  "sqs": "HTTPS", "celery": "AMQP/Redis", "s3": "S3 API", "llm": "HTTPS", "mcp": "MCP"}
IMAGE_KIND = [(r"nginx|traefik|envoy|haproxy|kong|caddy", "gateway"), (r"keycloak|authelia|dex", "security"),
              (r"prometheus|grafana|loki|jaeger|otel", "service")]


def _pretty(name: str) -> str:
    name = re.sub(r"[_\-]+", " ", name).strip()
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in name.split()) or "App"


def _under(path: str, d: str) -> bool:
    return not d or path == d or path.startswith(d + "/")


def _child(path: str, d: str) -> str | None:
    """The first directory below ``d`` that contains ``path`` (None for files directly in ``d``)."""
    rest = path[len(d) + 1:] if d else path
    parts = rest.split("/")
    return (f"{d}/{parts[0]}" if d else parts[0]) if len(parts) > 1 else None


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
        manifest = (mby_dir.get(d) or [None])[0]
        units[d] = {"dir": d, "files": sorted(paths), "symbols": sum(sym_count.get(p, 0) for p in paths),
                    "language": langs.most_common(1)[0][0], "manifest": manifest, "compose": None,
                    "frameworks": [], "ui": [], "entry_points": []}
    # Units with only a handful of files and no manifest are noise (stray scripts at the top level).
    for d in [d for d, u in units.items() if not u["manifest"] and u["symbols"] < 3 and len(units) > 1]:
        del units[d]
    return units


def unit_of_path(units: dict[str, dict[str, Any]], path: str) -> str | None:
    best = None
    for d, u in units.items():
        if path in u["_fileset"]:
            return d
        if _under(path, d) and (best is None or len(d) > len(best)):
            best = d
    return best


# --------------------------------------------------------------------------- reconstruction
def reconstruct(graph: dict[str, Any], *, repo_url: str | None = None, commit_sha: str | None = None,
                code_graph_id: str | None = None) -> dict[str, Any]:
    """Return {"nodes": [...], "edges": [...]} — node dicts ready for ``add_node`` ops (with ids)."""
    det = graph.get("detections") or {}
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
    compose = det.get("compose_services", [])
    for svc in compose:
        if svc.get("build") is not None:
            b = svc["build"]
            d = next((d for d in sorted(units, key=len, reverse=True) if _under(d, b) or _under(b, d)), None)
            if d is not None and not units[d]["compose"]:
                units[d]["compose"] = svc

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

    def new_id(name: str) -> str:
        base = slugify(name)
        nid, i = base, 2
        while nid in used_ids:
            nid, i = f"{base}-{i}", i + 1
        used_ids.add(nid)
        return nid

    # ---- units → services / clients
    unit_node: dict[str, str] = {}
    for d, u in sorted(units.items(), key=lambda kv: (-kv[1]["symbols"], kv[0])):
        is_client = bool(u["ui"]) and not u["frameworks"]
        if not is_client and u["manifest"] and u["manifest"].get("kind") == "node":
            deps = set(u["manifest"].get("dependencies") or [])
            is_client = bool(deps & {"react", "vue", "@angular/core", "svelte", "next", "react-native"}) and \
                not u["frameworks"]
        base = PurePosixPath(d).name if d else ""
        if u["compose"]:
            name = _pretty(u["compose"]["name"])
        elif base.lower() not in GENERIC_NAMES:
            name = _pretty(base)
        elif u["manifest"] and u["manifest"].get("name"):
            name = _pretty(str(u["manifest"]["name"]).split("/")[-1])
        else:
            name = _pretty(PurePosixPath(d).parent.name or graph.get("root_name") or "App")
        if is_client and "app" not in name.lower() and "client" not in name.lower() and "web" not in name.lower():
            name += " (web app)"
        tech_parts = (u["ui"] if is_client else u["frameworks"])[:2] + ([u["language"]] if u["language"] else [])
        technology = " · ".join(dict.fromkeys(tech_parts))
        entry = u["entry_points"][0]["path"] if u["entry_points"] else ""
        kind = "client" if is_client else "service"
        role = ("Front-end application" if is_client else
                "Service" if (u["frameworks"] or u["entry_points"] or u["compose"]) else "Library / module")
        desc = f"{role} reconstructed from {len(u['files'])} source file(s) in {d or 'the repository root'}"
        desc += f" ({u['symbols']} symbols)." + (f" Entry point: {entry}." if entry else "")
        code_paths = d or ", ".join(sorted({p.split("/")[0] for p in u["files"] if "/" in p})[:5]) or "."
        cat_entry = catalog.match({"technology": technology, "name": ""}) if is_client else None
        nid = new_id(name)
        unit_node[d] = nid
        nodes.append({"id": nid, "name": name, "type": kind, "technology": technology, "description": desc,
                      "catalog_id": cat_entry["id"] if cat_entry and cat_entry["kind"].startswith("client.") else "",
                      "vendor": cat_entry["vendor"] if cat_entry and cat_entry["kind"].startswith("client.") else "",
                      "props": {**common, "code_paths": code_paths, "symbols": str(u["symbols"]),
                                "files": str(len(u["files"])), **({"entry_point": entry} if entry else {})},
                      "_unit": d, "_frameworks": u["frameworks"], "_client": is_client})

    # ---- infrastructure
    infra_node: dict[str, str] = {}
    infra_items = list(det.get("infrastructure", []))
    seen_keys = {i.get("key") for i in infra_items}
    for svc in compose:  # compose images not already detected through code
        key = svc.get("infra") or (image_infra(svc.get("image", "")) if svc.get("build") is None else None)
        if key and key not in seen_keys and key in INFRA:
            label, category, ntype, cid, tech = INFRA[key]
            infra_items.append({"key": key, "label": label, "category": category, "type": ntype, "catalog_id": cid,
                                "technology": tech, "files": [], "evidence": [f"compose service '{svc['name']}'"]})
            seen_keys.add(key)
    for item in infra_items:
        key = item.get("key")
        if key not in INFRA:
            continue
        files = [p for p in item.get("files", []) if not is_test_path(p)]
        evidence = item.get("evidence") or []
        if not files and not evidence:
            continue
        cid = item.get("catalog_id") or ""
        entry = catalog.BY_ID.get(cid)
        paths = sorted({unit_of_path(units, p) or p.split("/")[0] for p in files})
        nid = new_id(item.get("label") or INFRA[key][0])
        infra_node[key] = nid
        desc = (f"Detected from {len(files)} file(s) importing its client library" if files else "Detected from deployment files")
        if evidence:
            desc += f"; {evidence[0]}"
        nodes.append({"id": nid, "name": item.get("label") or INFRA[key][0], "type": item.get("type") or INFRA[key][2],
                      "technology": item.get("technology") or (entry["name"] if entry else ""), "description": desc + ".",
                      "catalog_id": cid if entry else "", "vendor": entry["vendor"] if entry else "",
                      "props": {**common, "code_paths": ", ".join(p for p in paths if p)[:300] or "",
                                "files": str(len(files)), "detected_from": key}, "_infra": key})
        # unit → infra
        users = Counter(unit_of_path(units, p) for p in files)
        for d, n in users.items():
            if d is None or d not in unit_node:
                continue
            src, tgt = unit_node[d], nid
            if key == "mcp":
                src, tgt = nid, unit_node[d]
            edges.append({"source": src, "target": tgt, "protocol": INFRA_PROTOCOL.get(key, ""),
                          "label": f"{n} file(s) use it"})

    # ---- other compose containers (gateway, identity, monitoring, ...)
    for svc in compose:
        if svc.get("build") is not None or svc.get("infra") or image_infra(svc.get("image", "")):
            continue
        kind = next((k for pat, k in IMAGE_KIND if re.search(pat, svc.get("image", ""), re.I)), "service")
        nid = new_id(_pretty(svc["name"]))
        nodes.append({"id": nid, "name": _pretty(svc["name"]), "type": kind, "technology": svc.get("image", ""),
                      "description": f"Container '{svc['name']}' from {svc.get('file')}.", "catalog_id": "", "vendor": "",
                      "props": {**common, "code_paths": "", "compose_service": svc["name"]}, "_compose": svc["name"]})

    by_compose = {}
    for n in nodes:
        if n.get("_compose"):
            by_compose[n["_compose"]] = n["id"]
    for d, u in units.items():
        if u["compose"]:
            by_compose[u["compose"]["name"]] = unit_node[d]
    for svc in compose:
        key = svc.get("infra")
        if key and key in infra_node:
            by_compose[svc["name"]] = infra_node[key]

    # ---- compose depends_on
    for svc in compose:
        src = by_compose.get(svc["name"])
        for dep in svc.get("depends_on", []):
            tgt = by_compose.get(dep)
            if src and tgt and src != tgt:
                edges.append({"source": src, "target": tgt, "protocol": "", "label": "depends_on (compose)"})

    # ---- clients → web services (inferred)
    services = [n for n in nodes if n.get("_unit") is not None and not n.get("_client") and n.get("_frameworks")]
    for c in [n for n in nodes if n.get("_client")]:
        for s in services:
            proto = next((FRAMEWORK_PROTOCOL[f] for f in s["_frameworks"] if f in FRAMEWORK_PROTOCOL), "HTTP")
            edges.append({"source": c["id"], "target": s["id"], "protocol": proto, "label": "inferred"})

    # ---- code references across units
    file_unit = {}
    for s in graph.get("symbols", []):
        f = s["file"]
        if f not in file_unit:
            file_unit[f] = unit_of_path(units, f)
    sym_unit = {s["id"]: file_unit.get(s["file"]) for s in graph.get("symbols", [])}
    cross: Counter[tuple[str, str]] = Counter()
    for e in graph.get("edges", []):
        if e.get("relation") not in CODE_EDGE_RELATIONS:
            continue
        a, b = sym_unit.get(e["source"]), sym_unit.get(e["target"])
        if a is not None and b is not None and a != b and a in unit_node and b in unit_node:
            cross[(a, b)] += 1
    for (a, b), n in cross.most_common():
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
    return {"nodes": nodes, "edges": uniq}


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
            "edges": recon["edges"]}


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
                 "Return better human-friendly names (max 40 characters) and one-sentence descriptions for the given "
                 "components. Do not invent components; keep every id exactly as given.")


def refine(recon: dict[str, Any], graph: dict[str, Any], org_id: str | None = None) -> str:
    """Improve names/descriptions with the LLM when available. Returns the engine used ("llm" or "offline")."""
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
    for c in data.get("components", []):
        n = by_id.get(str(c.get("id")))
        if not n:
            continue
        name = str(c.get("name") or "").strip()
        if 0 < len(name) <= 60:
            n["name"] = name
        desc = str(c.get("description") or "").strip()
        if desc:
            n["description"] = desc[:400]
    return "llm"
