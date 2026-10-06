"""The only module that imports Graphify (``graphifyy==0.9.77``, optional extra ``codegraph``).

Runs Graphify's pipeline on a directory — tree-sitter extraction, graph build, Leiden communities, god nodes,
import cycles — and converts the result into ArchFlow's normalized graph (see ``codegraph/__init__.py``), merged
with the deterministic detections from ``detect.py``.

Call it from a real module (the worker or the CLI), never from ``python -c``: Graphify's parallel extraction
spawns processes that re-import ``__main__``.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections import Counter
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from graphify.analyze import find_import_cycles, god_nodes
from graphify.build import build
from graphify.cluster import cluster, label_communities_by_hub
from graphify.extract import collect_files, extract

from . import FORMAT, detect, normalize


def _max_files() -> int:
    try:
        return int(os.environ.get("ARCHFLOW_CODEGRAPH_MAX_FILES", "20000"))
    except ValueError:
        return 20000


def _line(loc: Any) -> int | None:
    m = re.match(r"L(\d+)", str(loc or ""))
    return int(m.group(1)) if m else None


def _kind(data: dict[str, Any]) -> str:
    label = str(data.get("label") or "")
    src = str(data.get("source_file") or "")
    if src and label == PurePosixPath(src).name:
        return "file"
    if data.get("_callable_class"):
        return "class"
    if label.startswith(".") and label.endswith("()"):
        return "method"
    if label.endswith("()"):
        return "function"
    if label[:1].isupper():
        return "class"
    return "symbol"


def _name(label: str) -> str:
    label = label.strip()
    if label.endswith("()"):
        label = label[:-2]
    return label.lstrip(".") or label


def analyze_directory(root: Path, cache_dir: Path | None = None) -> dict[str, Any]:
    """Analyse the tree at ``root`` and return a validated normalized graph."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    files = [p for p in collect_files(root) if detect.language_of(p.name) in detect.CODE_LANGS]
    files = files[: _max_files()]
    own_cache = cache_dir is None
    tmp = tempfile.TemporaryDirectory(prefix="archflow-graphify-") if own_cache else None
    cache_root = Path(tmp.name) if tmp else Path(cache_dir)
    try:
        extraction = extract(files, cache_root=cache_root, root=root) if files else {"nodes": [], "edges": []}
        G = build([extraction], directed=True, root=root)
    finally:
        if tmp:
            tmp.cleanup()
    communities = cluster(G) if G.number_of_nodes() else {}
    labels = label_communities_by_hub(G, communities) if communities else {}

    # ---- symbols: graph nodes that belong to a file of this repository (external stubs have no source_file)
    symbols: list[dict[str, Any]] = []
    known: set[str] = set()
    community_of = {nid: cid for cid, members in communities.items() for nid in members}
    per_file: Counter[str] = Counter()
    for nid, data in G.nodes(data=True):
        src = normalize.clean_path(data.get("source_file"))
        if not src:
            continue
        sid = str(nid)
        known.add(sid)
        kind = _kind(data)
        if kind != "file":
            per_file[src] += 1
        symbols.append({"id": sid, "name": _name(str(data.get("label") or sid)), "file": src,
                        "line": _line(data.get("source_location")), "kind": kind,
                        "community": community_of.get(nid)})

    edges: list[dict[str, Any]] = []
    for u, v, data in G.edges(data=True):
        if str(u) in known and str(v) in known:
            edges.append({"source": str(u), "target": str(v), "relation": str(data.get("relation") or "uses"),
                          "confidence": str(data.get("confidence") or "EXTRACTED")})

    file_of = {s["id"]: s["file"] for s in symbols}
    comm_records = []
    for cid, members in communities.items():
        mem = [m for m in members if str(m) in known]
        if not mem:
            continue
        dirs = Counter(str(PurePosixPath(file_of[str(m)]).parent) for m in mem)
        comm_records.append({"id": int(cid), "size": len(mem), "label": labels.get(cid, f"Community {cid}"),
                             "paths": [d if d != "." else "" for d, _ in dirs.most_common(3)]})

    names = {s["id"]: s["name"] for s in symbols}
    gods = [{"id": str(g["id"]), "name": names.get(str(g["id"]), str(g.get("label", ""))), "degree": int(g["degree"])}
            for g in god_nodes(G, top_n=15) if str(g["id"]) in known]
    cycles = [[normalize.clean_path(p) for p in c.get("cycle", [])] for c in find_import_cycles(G)]

    rels = [normalize.clean_path(str(PurePosixPath(*p.relative_to(root).parts))) for p in files]
    langs = {r: detect.language_of(r) or "" for r in rels}
    try:
        version = metadata.version("graphifyy")
    except metadata.PackageNotFoundError:  # pragma: no cover
        version = ""
    graph = {
        "format": FORMAT,
        "generator": {"tool": "graphify", "version": version},
        "root_name": root.name,
        "files": [{"path": r, "language": langs[r], "symbols": per_file.get(r, 0)} for r in rels],
        "symbols": symbols,
        "edges": edges,
        "communities": comm_records,
        "detections": detect.detect(root),
        "analysis": {"god_nodes": gods, "import_cycles": cycles},
    }
    return normalize.validate(graph)
