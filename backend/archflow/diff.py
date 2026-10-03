"""Structural diff between two project states (architecture + requirements)."""

from __future__ import annotations

from typing import Any

from .models import ProjectState

IGNORED = {"rev", "field_revs"}


def _diff_collection(old: dict[str, Any], new: dict[str, Any], label, ignore=frozenset()) -> dict[str, list]:
    added = [{"id": k, "name": label(v)} for k, v in new.items() if k not in old]
    removed = [{"id": k, "name": label(v)} for k, v in old.items() if k not in new]
    changed = []
    for k in old.keys() & new.keys():
        a = old[k].model_dump(exclude=IGNORED | ignore)
        b = new[k].model_dump(exclude=IGNORED | ignore)
        fields = [{"field": f, "from": a.get(f), "to": b.get(f)} for f in sorted(a.keys() | b.keys()) if a.get(f) != b.get(f)]
        if fields:
            changed.append({"id": k, "name": label(new[k]), "fields": fields})
    return {"added": added, "removed": removed, "changed": changed}


def diff_states(old: ProjectState, new: ProjectState, include_moves: bool = False) -> dict[str, Any]:
    names_old = {k: n.name for k, n in old.nodes.items()}
    names_new = {k: n.name for k, n in new.nodes.items()}

    def edge_label(names):
        return lambda e: f"{names.get(e.source, e.source)} → {names.get(e.target, e.target)}" + (f" ({e.protocol})" if e.protocol else "")

    nodes = _diff_collection(old.nodes, new.nodes, lambda n: n.name, frozenset() if include_moves else frozenset({"position"}))
    edges = {
        "added": [{"id": k, "name": edge_label(names_new)(e)} for k, e in new.edges.items() if k not in old.edges],
        "removed": [{"id": k, "name": edge_label(names_old)(e)} for k, e in old.edges.items() if k not in new.edges],
        "changed": _diff_collection(old.edges, new.edges, edge_label(names_new))["changed"],
    }
    reqs = _diff_collection(old.requirements, new.requirements, lambda r: f"{r.id} {r.title}")
    total = sum(len(section[k]) for section in (nodes, edges, reqs) for k in ("added", "removed", "changed"))
    return {"nodes": nodes, "edges": edges, "requirements": reqs, "total_changes": total}


def diff_to_text(d: dict[str, Any]) -> str:
    lines: list[str] = []
    for title, key in (("Components", "nodes"), ("Connections", "edges"), ("Requirements", "requirements")):
        section = d[key]
        if not any(section.values()):
            continue
        lines.append(f"{title}:")
        lines += [f"  + {x['name']}" for x in section["added"]]
        lines += [f"  - {x['name']}" for x in section["removed"]]
        for x in section["changed"]:
            for f in x["fields"]:
                lines.append(f"  ~ {x['name']}: {f['field']} {f['from']!r} → {f['to']!r}")
    return "\n".join(lines) or "No changes."
