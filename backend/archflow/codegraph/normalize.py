"""ArchFlow's normalized code-graph format: validation (incl. uploads), summaries and compression.

No Graphify import here: uploaded graphs and stored graphs are handled without the optional dependency.
"""

from __future__ import annotations

import json
import os
import re
import zlib
from typing import Any

from . import FORMAT
from .detect import merge_compose

MAX_FILES = 100_000
MAX_SYMBOLS = 200_000
MAX_EDGES = 600_000
MAX_COMMUNITIES = 5_000
SYMBOL_KINDS = {"file", "class", "function", "method", "symbol"}
CONFIDENCES = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}


def max_upload_bytes() -> int:
    try:
        return max(1, int(os.environ.get("ARCHFLOW_CODEGRAPH_MAX_UPLOAD_MB", "25"))) * 1024 * 1024
    except ValueError:
        return 25 * 1024 * 1024


def _s(value: Any, limit: int = 300) -> str:
    return str(value if value is not None else "")[:limit]


def clean_path(p: Any) -> str:
    """POSIX, relative, no traversal: 'a/b.py'."""
    s = _s(p, 500).replace("\\", "/")
    parts = [x for x in s.split("/") if x not in ("", ".", "..")]
    return "/".join(parts)


def validate(data: Any) -> dict[str, Any]:
    """Return a cleaned normalized graph or raise ValueError. Unknown keys are dropped, strings are capped and
    edges whose ends are not known symbols are removed."""
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    if data.get("format") != FORMAT:
        raise ValueError(f"unsupported format '{_s(data.get('format'), 60)}' (expected '{FORMAT}'); "
                         "produce it with the archflow-codegraph CLI")
    files_in, symbols_in, edges_in = data.get("files") or [], data.get("symbols") or [], data.get("edges") or []
    if not all(isinstance(x, list) for x in (files_in, symbols_in, edges_in)):
        raise ValueError("files, symbols and edges must be lists")
    if len(files_in) > MAX_FILES or len(symbols_in) > MAX_SYMBOLS or len(edges_in) > MAX_EDGES:
        raise ValueError(f"graph too large (limits: {MAX_FILES} files, {MAX_SYMBOLS} symbols, {MAX_EDGES} edges)")

    files = []
    for f in files_in:
        if isinstance(f, dict) and clean_path(f.get("path")):
            files.append({"path": clean_path(f.get("path")), "language": _s(f.get("language"), 40),
                          "symbols": int(f.get("symbols") or 0) if str(f.get("symbols") or 0).isdigit() else 0})
    symbols, ids = [], set()
    for s in symbols_in:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sid = _s(s["id"], 300)
        if sid in ids:
            continue
        ids.add(sid)
        kind = s.get("kind") if s.get("kind") in SYMBOL_KINDS else "symbol"
        comm = s.get("community")
        line = s.get("line")
        symbols.append({"id": sid, "name": _s(s.get("name") or sid, 200), "file": clean_path(s.get("file")),
                        "line": line if isinstance(line, int) and line >= 0 else None, "kind": kind,
                        "community": comm if isinstance(comm, int) else None})
    edges = []
    for e in edges_in:
        if not isinstance(e, dict):
            continue
        src, tgt = _s(e.get("source"), 300), _s(e.get("target"), 300)
        if src in ids and tgt in ids and src != tgt:
            conf = e.get("confidence") if e.get("confidence") in CONFIDENCES else "EXTRACTED"
            edges.append({"source": src, "target": tgt, "relation": re.sub(r"[^a-z_]", "", _s(e.get("relation"), 40)) or "uses",
                          "confidence": conf})
    communities = []
    for c in (data.get("communities") or [])[:MAX_COMMUNITIES]:
        if isinstance(c, dict) and isinstance(c.get("id"), int):
            communities.append({"id": c["id"], "size": int(c.get("size") or 0), "label": _s(c.get("label"), 200),
                                "paths": [clean_path(p) for p in (c.get("paths") or [])[:10]]})
    det = data.get("detections") if isinstance(data.get("detections"), dict) else {}
    detections = _clean_detections(det)
    analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    gen = data.get("generator") if isinstance(data.get("generator"), dict) else {}
    out = {
        "format": FORMAT,
        "generator": {"tool": _s(gen.get("tool"), 40), "version": _s(gen.get("version"), 40)},
        "root_name": _s(data.get("root_name"), 120),
        "files": files, "symbols": symbols, "edges": edges, "communities": communities,
        "detections": detections,
        "analysis": {
            "god_nodes": [{"id": _s(g.get("id")), "name": _s(g.get("name")), "degree": int(g.get("degree") or 0)}
                          for g in (analysis.get("god_nodes") or [])[:50] if isinstance(g, dict)],
            "import_cycles": [[clean_path(p) for p in cyc[:20]] for cyc in (analysis.get("import_cycles") or [])[:50]
                              if isinstance(cyc, list)],
        },
    }
    out["stats"] = {"files": len(files), "symbols": len(symbols), "edges": len(edges), "communities": len(communities)}
    return out


def _clean_detections(det: dict[str, Any]) -> dict[str, Any]:
    """Detections are kept as-is structurally but bounded: they are JSON produced by our own detector, possibly
    edited by a user, so only types and sizes are enforced."""
    def lst(key: str, limit: int = 500) -> list[dict[str, Any]]:
        v = det.get(key)
        return [x for x in v[:limit] if isinstance(x, dict)] if isinstance(v, list) else []

    langs = det.get("languages") if isinstance(det.get("languages"), dict) else {}
    out = {
        "languages": {_s(k, 40): int(v) for k, v in list(langs.items())[:60] if isinstance(v, int)},
        "manifests": lst("manifests"), "compose_services": lst("compose_services"), "dockerfiles": lst("dockerfiles"),
        "k8s": lst("k8s"), "terraform": lst("terraform"), "proxy_routes": lst("proxy_routes", 50),
        "infrastructure": lst("infrastructure", 100),
        "frameworks": lst("frameworks", 100), "entry_points": lst("entry_points", 300),
    }
    for it in out["infrastructure"]:
        it["files"] = [clean_path(p) for p in (it.get("files") or [])[:200] if isinstance(p, str)]
    for it in out["frameworks"]:
        it["files"] = [clean_path(p) for p in (it.get("files") or [])[:200] if isinstance(p, str)]
    for m in out["manifests"]:
        m["path"] = clean_path(m.get("path"))
        m["dir"] = clean_path(m.get("dir"))
    for c in out["compose_services"]:
        if c.get("build") is not None:
            c["build"] = clean_path(c.get("build"))
        if c.get("dockerfile"):
            c["dockerfile"] = clean_path(c.get("dockerfile"))
    for r in out["proxy_routes"]:
        r["path"] = clean_path(r.get("path"))
        r["targets"] = [_s(t, 120) for t in (r.get("targets") or [])[:30] if isinstance(t, str)]
    return out


def summarize(graph: dict[str, Any]) -> dict[str, Any]:
    """Compact overview for listings, the UI and MCP."""
    det = graph.get("detections") or {}
    comms = sorted(graph.get("communities") or [], key=lambda c: -c.get("size", 0))
    compose = merge_compose(det.get("compose_services", []))  # also merges graphs stored before merging existed
    return {
        "stats": graph.get("stats") or {},
        "generator": graph.get("generator") or {},
        "root_name": graph.get("root_name", ""),
        "languages": det.get("languages", {}),
        "infrastructure": [{"key": i.get("key"), "label": i.get("label"), "category": i.get("category"),
                            "technology": i.get("technology"), "files": len(i.get("files") or []),
                            "evidence": (i.get("evidence") or [])[:5]} for i in det.get("infrastructure", [])],
        "frameworks": [{"name": f.get("name"), "kind": f.get("kind"), "files": len(f.get("files") or [])}
                       for f in det.get("frameworks", [])],
        "entry_points": det.get("entry_points", [])[:30],
        "manifests": [{"path": m.get("path"), "kind": m.get("kind"), "name": m.get("name")} for m in det.get("manifests", [])],
        "compose_services": [{"name": c.get("name"), "image": c.get("image"), "build": c.get("build"),
                              "files": c.get("files", []), "dev_tool": bool(c.get("dev_tool"))} for c in compose],
        "dev_tools": [{"name": c.get("name"), "image": c.get("image"), "files": c.get("files", [])}
                      for c in compose if c.get("dev_tool")],
        "dockerfiles": len(det.get("dockerfiles", [])),
        "k8s": len(det.get("k8s", [])),
        "terraform": len(det.get("terraform", [])),
        "communities": [{"id": c["id"], "size": c.get("size", 0), "label": c.get("label", ""), "paths": c.get("paths", [])[:3]}
                        for c in comms[:12]],
        "god_nodes": (graph.get("analysis") or {}).get("god_nodes", [])[:10],
        "import_cycles": len((graph.get("analysis") or {}).get("import_cycles", [])),
    }


def compress(graph: dict[str, Any]) -> bytes:
    return zlib.compress(json.dumps(graph, separators=(",", ":")).encode("utf-8"), 6)


def decompress(blob: bytes | memoryview | None) -> dict[str, Any] | None:
    if blob is None:
        return None
    return json.loads(zlib.decompress(bytes(blob)).decode("utf-8"))
