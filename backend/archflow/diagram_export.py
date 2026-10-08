"""Export a design as an interactive diagram: a standalone, ArchFlow-branded HTML file.

Rendering is done by ArchFlow's own renderer in ``backend/renderer`` (Node.js, forked from Archify 3.0.1, MIT; see
``backend/renderer/README.md``). It turns a typed JSON description into a self-contained viewer with node
exploration, path tracing, themes and PNG / SVG / WebM export. The renderer does no automatic layout; ArchFlow
already has a position for every component, so the conversion here is direct:

* ``architecture_ir``: one diagram level (the system, or a component's internals) → ``architecture`` IR.
  ArchFlow types map onto the renderer's seven component types; technologies with a known logo get ``brand``.
* ``sequence_ir``: a component's main request flow (``lld.sequence_steps``) → ``sequence`` IR.

``render`` runs ``node renderer/bin/archflow.mjs render <type> in.json out.html`` in a temporary directory, on
the API server (nothing leaves it). The renderer validates layout strictly (overlaps, crossings, label room);
its suggested label positions are applied, and a rejected hand-made layout is retried with more spacing, then
on a tidy grid. Needs Node.js 18+ (``ARCHFLOW_NODE`` to override the binary).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import Node, ProjectState

log = logging.getLogger("archflow.export")

RENDERER_DIR = Path(__file__).resolve().parent.parent / "renderer"
CLI = RENDERER_DIR / "bin" / "archflow.mjs"
TIMEOUT = int(os.environ.get("ARCHFLOW_EXPORT_TIMEOUT", "60"))

# ArchFlow node type → renderer component type (frontend, backend, database, cloud, security, messagebus, external).
TYPE_MAP = {
    "client": "frontend", "gateway": "cloud", "service": "backend", "database": "database", "cache": "database",
    "queue": "messagebus", "storage": "cloud", "agent": "backend", "llm": "external", "mcp": "backend",
    "external": "external", "security": "security", "component": "backend", "endpoint": "backend",
    "table": "database", "event": "messagebus",
}
NODE_W, NODE_H, MAX_W = 180, 64, 280
MARGIN = 40


def node_width(label: str, branded: bool) -> int:
    """Box width that fits the label (the renderer rejects labels wider than their box; a logo takes ~48px)."""
    return min(MAX_W, max(NODE_W, round(len(label) * 7.2) + (64 if branded else 24)))


class ExportError(RuntimeError):
    """The diagram could not be rendered (details in the message)."""


class ExportUnavailable(ExportError):
    """Node.js or the renderer is missing."""


def node_binary() -> str | None:
    return shutil.which(os.environ.get("ARCHFLOW_NODE", "node"))


def available() -> bool:
    return CLI.is_file() and node_binary() is not None


# --------------------------------------------------------------------------- logos
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9.+#]+", " ", (text or "").lower()).strip()


@lru_cache(maxsize=1)
def brand_keys() -> dict[str, str]:
    """Normalized title / id / alias → renderer brand id, from its logo catalogue."""
    try:
        src = (RENDERER_DIR / "renderers" / "shared" / "generated-brand-marks.mjs").read_text(encoding="utf-8")
        start = src.index("Object.freeze([") + len("Object.freeze(")
        marks = json.loads(src[start:src.index("]);", start) + 1])
    except (OSError, ValueError) as exc:
        log.warning("renderer logo catalogue unavailable: %s", exc)
        return {}
    keys: dict[str, str] = {}
    for m in marks:
        for k in (m["id"], m["id"].replace("-", " "), m["title"], *m.get("aliases", [])):
            if _norm(k):
                keys.setdefault(_norm(k), m["id"])
    return keys


def brand_for(*texts: str) -> str | None:
    """The logo for a component's technology or name: an exact match, else the longest whole-word match
    (at least 4 characters, so "Go" or "X" only match on their own)."""
    keys = brand_keys()
    for text in texts:
        n = _norm(text)
        if not n:
            continue
        if n in keys:
            return keys[n]
        hits = [k for k in keys if len(k) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", n)]
        if hits:
            return keys[max(hits, key=len)]
    return None


# --------------------------------------------------------------------------- IR builders
def _ids() -> Any:
    used: dict[str, str] = {}

    def make(raw: str) -> str:
        if raw in used:
            return used[raw]
        base = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-") or "n"
        if not base[0].isalpha():
            base = f"n-{base}"
        cand, i = base, 2
        while cand in used.values():
            cand, i = f"{base}-{i}", i + 1
        used[raw] = cand
        return cand
    return make


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def level_nodes(state: ProjectState, level: str = "") -> list[Node]:
    return [n for n in state.nodes.values() if n.parent_id == level and n.type != "note"]


def architecture_ir(state: ProjectState, title: str, level: str = "", *, layout: str = "authored",
                    spread: float = 1.0) -> dict[str, Any]:
    """``layout``: ``authored`` keeps canvas positions (scaled by ``spread``); ``grid`` places components on
    rows by their vertical order (used when the renderer rejects the hand-made layout)."""
    nodes = level_nodes(state, level)
    if not nodes:
        raise ExportError("There is nothing to export on this diagram level.")
    make_id = _ids()
    comps = []
    if layout == "grid":
        rows: list[list[Node]] = []
        for n in sorted(nodes, key=lambda n: (n.position.y, n.position.x)):
            if rows and abs(rows[-1][0].position.y - n.position.y) < 90:
                rows[-1].append(n)
            else:
                rows.append([n])
        col_w = max(node_width(_clip(n.name, 36), brand_for(n.technology, n.name) is not None) for n in nodes) + 110
        placed = {n.id: (MARGIN + c * col_w, MARGIN + r * (NODE_H + 110))
                  for r, row in enumerate(rows) for c, n in enumerate(sorted(row, key=lambda n: n.position.x))}
    else:
        min_x = min(n.position.x for n in nodes)
        min_y = min(n.position.y for n in nodes)
        placed = {n.id: (MARGIN + round((n.position.x - min_x) * spread), MARGIN + round((n.position.y - min_y) * spread))
                  for n in nodes}
    for n in nodes:
        label, brand = _clip(n.name, 36), brand_for(n.technology, n.name)
        width = node_width(label, brand is not None)
        comp: dict[str, Any] = {"id": make_id(n.id), "type": TYPE_MAP.get(n.type, "backend"), "label": label,
                                "pos": list(placed[n.id]), "size": [width, NODE_H]}
        if n.technology:
            comp["sublabel"] = _clip(n.technology, max(20, (width - 20) // 6))
        if brand:
            comp["brand"] = brand
        comps.append(comp)
    ids = {n.id for n in nodes}
    conns, seen = [], set()
    for e in state.edges.values():
        if e.source in ids and e.target in ids and (e.source, e.target) not in seen:
            seen.add((e.source, e.target))
            c: dict[str, Any] = {"from": make_id(e.source), "to": make_id(e.target)}
            label = _clip(e.label or e.protocol, 22)
            if label:
                c["label"] = label
            conns.append(c)
    return {"schema_version": 1, "diagram_type": "architecture",
            "meta": {"title": _clip(title, 90), "output": "diagram.html", "quality_profile": "standard"},
            "components": comps, "connections": conns}


def sequence_ir(steps: dict[str, Any], title: str) -> dict[str, Any]:
    """``steps`` from lld.sequence_steps: participants [{id, name, type, technology}] and messages
    [{from, to, label, kind: call|async|return}]."""
    if len(steps.get("participants", [])) < 2 or not steps.get("messages"):
        raise ExportError("This component has no request flow to export yet (it needs callers or dependencies).")
    make_id = _ids()
    parts = []
    for p in steps["participants"]:
        part: dict[str, Any] = {"id": make_id(p["id"]), "type": TYPE_MAP.get(p["type"], "backend"),
                                "label": _clip(p["name"], 28)}
        if p.get("technology"):
            part["sublabel"] = _clip(p["technology"], 24)
        brand = brand_for(p.get("technology", ""), p["name"])
        if brand:
            part["brand"] = brand
        parts.append(part)
    msgs, y = [], 170
    for i, m in enumerate(steps["messages"]):
        msg: dict[str, Any] = {"id": f"m{i + 1}", "from": make_id(m["from"]), "to": make_id(m["to"]), "y": y,
                               "label": _clip(m["label"], 40) or "call"}
        if m["kind"] == "return":
            msg["variant"] = "return"
        elif m["kind"] == "async":
            msg["variant"] = "dashed"
        msgs.append(msg)
        y += 46
    return {"schema_version": 1, "diagram_type": "sequence",
            "meta": {"title": _clip(title, 90), "output": "diagram.html", "quality_profile": "standard",
                     "column_fit": "spread", "viewBox": [max(720, 170 * len(parts)), y + 60]},
            "participants": parts, "messages": msgs}


# --------------------------------------------------------------------------- rendering
def render(ir: dict[str, Any]) -> str:
    """Render IR to standalone HTML; raises ExportError with the renderer's diagnostics if it is rejected."""
    node = node_binary()
    if not CLI.is_file() or node is None:
        raise ExportUnavailable("Interactive export needs Node.js 18+ on the API server (and backend/renderer).")
    with tempfile.TemporaryDirectory(prefix="archflow-export-") as tmp:
        src, out = Path(tmp) / "diagram.json", Path(tmp) / "diagram.html"
        src.write_text(json.dumps(ir), encoding="utf-8")
        try:
            proc = subprocess.run([node, str(CLI), "render", ir["diagram_type"], str(src), str(out), "--quality", "standard"],
                                  cwd=tmp, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=TIMEOUT, env={**os.environ, "NO_COLOR": "1"})
        except subprocess.TimeoutExpired as exc:
            raise ExportError(f"Rendering took longer than {TIMEOUT}s.") from exc
        if proc.returncode != 0 or not out.is_file():
            detail = "\n".join(line for line in (proc.stderr or proc.stdout).splitlines() if line.strip())[:1500]
            raise ExportError(detail or f"The renderer exited with code {proc.returncode}")
        return out.read_text(encoding="utf-8")


_LABEL_PROBLEM = re.compile(
    r'Label "(?P<label>[^"]+)" overlaps[^\n]*\n\s*label rect: \[(?P<x>-?[\d.]+), (?P<y>-?[\d.]+), (?P<w>[\d.]+), (?P<h>[\d.]+)\]'
    r'(?:(?!\n- |\n\[).)*?Suggested fix: set labelAt \[(?P<ax>-?[\d.]+), (?P<ay>-?[\d.]+)\]', re.S)
_LABEL_ONLY = re.compile(r'Label "(?P<label>[^"]+)" overlaps')


def _label_owner(ir: dict[str, Any], label: str, cx: float, cy: float) -> dict[str, Any] | None:
    """The connection carrying ``label`` whose midpoint is nearest to (cx, cy)."""
    centers = {c["id"]: (c["pos"][0] + c["size"][0] / 2, c["pos"][1] + c["size"][1] / 2) for c in ir["components"]}
    owners = [c for c in ir["connections"] if c.get("label") == label]

    def dist(c: dict[str, Any]) -> float:
        (x1, y1), (x2, y2) = centers[c["from"]], centers[c["to"]]
        return ((x1 + x2) / 2 - cx) ** 2 + ((y1 + y2) / 2 - cy) ** 2
    return min(owners, key=dist) if owners else None


def repair_labels(ir: dict[str, Any], diagnostics: str, *, drop: bool = False) -> bool:
    """Apply the renderer's suggested ``labelAt`` for each label it reports as overlapping (or, with ``drop``,
    remove those labels). Returns True if anything changed."""
    changed = False
    for m in _LABEL_PROBLEM.finditer(diagnostics):
        cx, cy = float(m["x"]) + float(m["w"]) / 2, float(m["y"]) + float(m["h"]) / 2
        conn = _label_owner(ir, m["label"], cx, cy)
        if conn is None:
            continue
        if drop:
            changed |= conn.pop("label", None) is not None
            conn.pop("labelAt", None)
        elif conn.get("labelAt") != [float(m["ax"]), float(m["ay"])]:
            conn["labelAt"] = [float(m["ax"]), float(m["ay"])]
            changed = True
    if drop and not changed:  # reported without a suggestion: drop every label with that text
        for m in _LABEL_ONLY.finditer(diagnostics):
            for conn in ir["connections"]:
                if conn.get("label") == m["label"]:
                    conn.pop("label", None)
                    conn.pop("labelAt", None)
                    changed = True
    return changed


def export_architecture(state: ProjectState, title: str, level: str = "") -> tuple[str, str]:
    """(html, layout used). Tries the canvas layout, then more spacing, then a grid; for each, applies
    the renderer's suggested label positions and, if labels still collide, drops those labels."""
    last = ""
    for layout, spread in (("authored", 1.0), ("authored", 1.4), ("grid", 1.0)):
        ir = architecture_ir(state, title, level, layout=layout, spread=spread)
        for attempt in range(6):
            try:
                return render(ir), layout if spread == 1.0 else f"{layout}-spread"
            except ExportUnavailable:
                raise
            except ExportError as exc:
                last = str(exc)
                if repair_labels(ir, last, drop=attempt >= 3):
                    continue
                log.info("renderer rejected %s layout (spread %.1f): %s", layout, spread, last[:300])
                break
    raise ExportError("Could not lay out this diagram:\n" + last)


def export_sequence(steps: dict[str, Any], title: str) -> str:
    return render(sequence_ir(steps, title))
