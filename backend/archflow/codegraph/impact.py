"""Change impact: changed files → affected symbols → components → traced requirements.

Mirrors Graphify's ``affected_nodes`` (graphify/affected.py) on ArchFlow's stored normalized graph, so it runs in
the API without the optional dependency: seed with the symbols defined in the changed files (plus their members),
then walk *incoming* dependency edges (calls, imports, references, inheritance, ...) breadth-first up to ``depth``
hops — whoever calls/imports a changed symbol may break.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..models import ProjectState
from . import drift as drift_mod
from .normalize import clean_path

AFFECTED_RELATIONS = {"calls", "indirect_call", "references", "imports", "imports_from", "dynamic_import",
                      "re_exports", "inherits", "extends", "implements", "uses", "mixes_in", "embeds", "requires"}
MEMBER_RELATIONS = {"method", "contains"}
MAX_LIST = 300


def path_match(graph_file: str, changed: str) -> bool:
    """Same file, boundary-safe either way (a/b/c.py ~ b/c.py)."""
    return graph_file == changed or graph_file.endswith("/" + changed) or changed.endswith("/" + graph_file)


def affected_symbols(graph: dict[str, Any], files: list[str], depth: int = 2) -> dict[str, Any]:
    symbols = {s["id"]: s for s in graph.get("symbols", [])}
    changed = [clean_path(f) for f in files if clean_path(f)]
    graph_files = {s["file"] for s in symbols.values()} | {f["path"] for f in graph.get("files", [])}
    matched_files = {gf for gf in graph_files for c in changed if path_match(gf, c)}
    unmatched = [c for c in changed if not any(path_match(gf, c) for gf in graph_files)]

    incoming: dict[str, list[tuple[str, str]]] = {}
    members: dict[str, list[str]] = {}
    for e in graph.get("edges", []):
        rel = e.get("relation", "")
        if rel in AFFECTED_RELATIONS:
            incoming.setdefault(e["target"], []).append((e["source"], rel))
        elif rel in MEMBER_RELATIONS:
            members.setdefault(e["source"], []).append(e["target"])

    seeds = [sid for sid, s in symbols.items() if s["file"] in matched_files]
    seen = set(seeds)
    queue: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
    for s in seeds:
        for m in members.get(s, []):
            if m not in seen:
                seen.add(m)
                queue.append((m, 0))
    hits: list[dict[str, Any]] = []
    while queue:
        cur, d = queue.popleft()
        if d >= depth:
            continue
        for src, rel in incoming.get(cur, []):
            if src in seen:
                continue
            seen.add(src)
            s = symbols[src]
            hits.append({"id": src, "name": s["name"], "file": s["file"], "line": s.get("line"), "depth": d + 1,
                         "via": rel, "kind": s.get("kind")})
            queue.append((src, d + 1))
    affected_files = sorted(matched_files | {h["file"] for h in hits})
    return {"changed_files": sorted(matched_files), "unmatched_files": unmatched, "seed_symbols": len(seeds),
            "affected_symbols": hits, "affected_files": affected_files}


def code_components(recon: dict[str, Any], graph: dict[str, Any], files: list[str]) -> list[str]:
    """Reconstructed component ids touched by ``files``: units by directory, infrastructure by the files that
    import its client library."""
    infra_files = {i.get("key"): set(i.get("files") or []) for i in (graph.get("detections") or {}).get("infrastructure", [])}
    out = []
    for n in recon["nodes"]:
        if n.get("_unit") is not None:
            d = n["_unit"]
            if any(not d or f == d or f.startswith(d + "/") for f in files):
                out.append(n["id"])
        elif n.get("_infra"):
            if infra_files.get(n["_infra"], set()) & set(files):
                out.append(n["id"])
    return out


def impact(graph: dict[str, Any], recon: dict[str, Any], code_state: ProjectState, designed: ProjectState,
           files: list[str], depth: int = 2) -> dict[str, Any]:
    res = affected_symbols(graph, files, depth)
    touched = res["affected_files"]
    code_ids = code_components(recon, graph, touched)
    matches = drift_mod.match(designed, code_state)
    designed_ids: list[str] = [did for did, (cid, _) in matches.items() if cid in code_ids]
    for n in designed.nodes.values():  # explicit traceability on the designed component
        paths = drift_mod._paths(n)
        if paths and n.id not in designed_ids and any(
                p == f or f.startswith(p + "/") for p in paths for f in touched):
            designed_ids.append(n.id)
    req_ids: list[str] = []
    for did in designed_ids:
        for rid in designed.nodes[did].requirement_ids:
            if rid in designed.requirements and rid not in req_ids:
                req_ids.append(rid)
    code_names = {n["id"]: n["name"] for n in recon["nodes"]}
    return {
        **{k: v for k, v in res.items() if k != "affected_symbols"},
        "affected_symbols": res["affected_symbols"][:MAX_LIST],
        "affected_symbol_count": len(res["affected_symbols"]),
        "affected_files": touched[:MAX_LIST],
        "code_components": [{"id": c, "name": code_names.get(c, c)} for c in code_ids],
        "components": [{"id": d, "name": designed.nodes[d].name, "type": designed.nodes[d].type} for d in designed_ids],
        "requirements": [{"id": r, "title": designed.requirements[r].title, "priority": designed.requirements[r].priority,
                          "status": designed.requirements[r].status} for r in req_ids],
    }
