"""Drift: the designed architecture (a branch) versus the architecture reconstructed from code.

Matching (each designed top-level component picks its best code component; one code component may back several
designed ones, e.g. one PostgreSQL client library for "Orders DB" and "Users DB"):

* ``props.code_paths`` overlap (explicit traceability)                    +5
* same catalog entry / same catalog kind (catalog.match on both)           +4 / +3
* technology words in common (Jaccard)                                     +2 × j
* name similarity (difflib ratio ≥ 0.75, or shared words)                  up to +3
* types must be compatible (compute ↔ compute, data ↔ data/cache, ...); minimum score 2.

Issues (same shape as analysis issues; ``node_ids`` are designed component ids so the UI can focus them):

* ``drift_code_only``     — in code, not in the design (a datastore used by a designed service is called out);
* ``drift_design_only``   — designed, but no code was found for it;
* ``drift_connection``    — code connects two components the design does not connect (directly or via one
                            gateway/queue hop); inferred client→service links are not checked;
* ``drift_technology``    — matched, but the design names another technology (e.g. MySQL vs PostgreSQL).
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from .. import catalog
from ..analysis import diagram_of
from ..models import Node, ProjectState

GROUP = {"client": "client", "service": "compute", "agent": "compute", "mcp": "compute", "gateway": "edge",
         "security": "edge", "external": "external", "database": "data", "storage": "data", "cache": "cache",
         "queue": "queue", "llm": "llm"}
COMPATIBLE = {("data", "cache"), ("cache", "data"), ("compute", "edge"), ("edge", "compute"), ("llm", "external"),
              ("external", "llm"), ("compute", "external"), ("external", "compute")}
SKIP_DESIGN_ONLY = {"note", "external", "gateway", "security"}
HOP_TYPES = {"gateway", "queue", "security", "mcp", "cache"}
INFRA_GROUPS = {"data", "cache", "queue", "llm"}
_STOP = {"service", "services", "app", "api", "the", "and", "server", "web", "db", "database", "component", "module",
         "python", "typescript", "javascript", "reconstructed"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1 and w not in _STOP}


def _paths(node: Node) -> list[str]:
    return [p.strip().strip("/") for p in (node.props.get("code_paths") or "").split(",") if p.strip().strip("/")]


def paths_overlap(a: list[str], b: list[str]) -> bool:
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in a for y in b)


def _compatible(d: Node, c: Node) -> bool:
    gd, gc = GROUP.get(d.type, "other"), GROUP.get(c.type, "other")
    return gd == gc or (gd, gc) in COMPATIBLE


def score(d: Node, c: Node) -> float:
    if not _compatible(d, c):
        return 0.0
    s = 0.0
    dp, cp = _paths(d), _paths(c)
    if dp and cp and paths_overlap(dp, cp):
        s += 5
    de, ce = catalog.match(d.model_dump()), catalog.match(c.model_dump())
    if de and ce and de["vendor"] != "generic" and ce["vendor"] != "generic":
        if de["id"] == ce["id"]:
            s += 4
        elif de["kind"] and de["kind"] == ce["kind"]:
            s += 3
    dt, ct = _words(d.technology), _words(c.technology)
    if dt and ct:
        s += 2 * len(dt & ct) / len(dt | ct)
    # the designed name often names the technology ("Redis cache", "Postgres")
    if ct and _words(d.name) & ct:
        s += 1.5
    ratio = difflib.SequenceMatcher(None, d.name.lower(), c.name.lower()).ratio()
    if ratio >= 0.75:
        s += 3 * ratio
    else:
        dn, cn = _words(d.name), _words(c.name)
        if dn and cn:
            s += 1.5 * len(dn & cn) / len(dn | cn)
    return s


def match(designed: ProjectState, code: ProjectState, threshold: float = 2.0) -> dict[str, tuple[str, float]]:
    """designed node id → (code node id, score) for designed top-level components that have code."""
    out: dict[str, tuple[str, float]] = {}
    code_nodes = [c for c in code.nodes.values() if not c.parent_id]
    for d in designed.nodes.values():
        if diagram_of(designed, d) or d.type == "note":
            continue
        best, best_s = None, 0.0
        for c in code_nodes:
            sc = score(d, c)
            if sc > best_s:
                best, best_s = c, sc
        if best is not None and best_s >= threshold:
            out[d.id] = (best.id, round(best_s, 2))
    return out


def _issue(severity: str, code: str, message: str, suggestion: str = "", node_ids: list[str] | None = None,
           code_node_ids: list[str] | None = None) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "suggestion": suggestion,
            "node_ids": node_ids or [], "requirement_ids": [], "code_node_ids": code_node_ids or []}


def _connected(designed: ProjectState, a_ids: list[str], b_ids: list[str]) -> bool:
    adj: dict[str, set[str]] = {}
    for e in designed.edges.values():
        adj.setdefault(e.source, set()).add(e.target)
        adj.setdefault(e.target, set()).add(e.source)
    targets = set(b_ids)
    for a in a_ids:
        for n in adj.get(a, ()):
            if n in targets:
                return True
            node = designed.nodes.get(n)
            if node and node.type in HOP_TYPES and adj.get(n, set()) & targets:
                return True
    return False


def drift(designed: ProjectState, code: ProjectState) -> dict[str, Any]:
    matches = match(designed, code)
    by_code: dict[str, list[str]] = {}
    for did, (cid, _) in matches.items():
        by_code.setdefault(cid, []).append(did)
    issues: list[dict[str, Any]] = []

    # ---- in code, not designed
    code_only = []
    for c in code.nodes.values():
        if c.parent_id or c.id in by_code:
            continue
        code_only.append(c.id)
        where = c.props.get("code_paths") or ""
        users = [e.source for e in code.edges.values() if e.target == c.id and e.label != "inferred"]
        designed_users = [d for u in users for d in by_code.get(u, [])]
        infra = GROUP.get(c.type) in INFRA_GROUPS
        if designed_users:
            names = ", ".join(sorted({designed.nodes[d].name for d in designed_users}))
            msg = f"{names} talk{'s' if len(set(designed_users)) == 1 else ''} to {c.name} in code, but the design has no {c.name}."
        else:
            msg = f"Code has {c.type} '{c.name}'" + (f" ({where})" if where else "") + " that is not in the design."
        issues.append(_issue("warning", "drift_code_only", msg,
                             f"Add {'a' if infra else 'the'} {c.type} '{c.name}' to the design, or remove it from the code.",
                             node_ids=sorted(set(designed_users)), code_node_ids=[c.id]))

    # ---- designed, not in code
    design_only = []
    for d in designed.nodes.values():
        if diagram_of(designed, d) or d.type in SKIP_DESIGN_ONLY or d.id in matches:
            continue
        design_only.append(d.id)
        infra = GROUP.get(d.type) in INFRA_GROUPS
        issues.append(_issue("warning" if infra else "info", "drift_design_only",
                             f"'{d.name}' ({d.type}) is designed but no matching code was found.",
                             "Implement it, link it with props.code_paths, or remove it from the design.",
                             node_ids=[d.id]))

    # ---- undeclared connections
    reported: set[tuple[str, str]] = set()
    for e in code.edges.values():
        if e.label == "inferred":
            continue
        a_ids, b_ids = by_code.get(e.source, []), by_code.get(e.target, [])
        if not a_ids or not b_ids or set(a_ids) & set(b_ids):
            continue
        if _connected(designed, a_ids, b_ids):
            continue
        key = (a_ids[0], b_ids[0])
        if key in reported:
            continue
        reported.add(key)
        a, b = designed.nodes[a_ids[0]], designed.nodes[b_ids[0]]
        issues.append(_issue("warning", "drift_connection",
                             f"Code connects '{a.name}' → '{b.name}' ({e.protocol or 'code'}: {e.label}), "
                             "but the design does not.",
                             "Add the connection to the design, or remove the dependency from the code.",
                             node_ids=[a.id, b.id], code_node_ids=[e.source, e.target]))

    # ---- technology mismatches
    for did, (cid, _) in matches.items():
        d, c = designed.nodes[did], code.nodes[cid]
        de, ce = catalog.match(d.model_dump()), catalog.match(c.model_dump())
        mismatch = False
        if de and ce and de["kind"] and de["kind"] == ce["kind"] and de["id"] != ce["id"] \
                and not (_words(d.technology) & _words(c.technology)):
            mismatch = True
        elif GROUP.get(d.type) in INFRA_GROUPS and d.technology and c.technology and de and ce \
                and de["vendor"] != "generic" and de["kind"] != ce["kind"]:
            mismatch = True
        if mismatch:
            issues.append(_issue("warning", "drift_technology",
                                 f"'{d.name}' is designed as {d.technology or de['name']} but the code uses "
                                 f"{c.technology or ce['name']}.",
                                 "Align the design with the code (or plan the migration).",
                                 node_ids=[d.id], code_node_ids=[c.id]))

    candidates = [d for d in designed.nodes.values()
                  if not diagram_of(designed, d) and d.type not in SKIP_DESIGN_ONLY]
    total = len(candidates) + len([c for c in code.nodes.values() if not c.parent_id])
    matched = len([d for d in candidates if d.id in matches]) + len(by_code)
    penalty = 5 * sum(1 for i in issues if i["code"] in ("drift_connection", "drift_technology"))
    alignment = max(0, round(100 * matched / total) - penalty) if total else 100
    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["severity"]], i["code"]))
    return {
        "issues": issues,
        "matches": [{"node_id": did, "name": designed.nodes[did].name, "code_node_id": cid,
                     "code_name": code.nodes[cid].name, "score": sc} for did, (cid, sc) in matches.items()],
        "code_only": code_only, "design_only": design_only, "alignment": alignment,
    }
