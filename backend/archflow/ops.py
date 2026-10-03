"""The single mutation pipeline for a project branch.

Humans (canvas UI), the AI command endpoint and MCP agents all submit lists of
operations. A batch is applied atomically: either every op succeeds or nothing changes.

Operation shapes::

    {"op": "add_node", "node": {...Node fields...}}
    {"op": "update_node", "id": "<id or name>", "changes": {...}, "base_rev": 12}
    {"op": "remove_node", "id": "<id or name>"}
    {"op": "add_edge", "edge": {"source": ..., "target": ..., "protocol": ...}}
    {"op": "update_edge", "id": ..., "changes": {...}, "base_rev": 12}
    {"op": "remove_edge", "id": ...}   # or {"op": "remove_edge", "source": ..., "target": ...}
    {"op": "add_requirement", "requirement": {...}}
    {"op": "update_requirement", "id": "REQ-001", "changes": {...}, "base_rev": 12}
    {"op": "remove_requirement", "id": "REQ-001"}

``base_rev`` is the project revision the client last saw. If a field in ``changes`` was
modified by someone else after that revision (and to a different value), the batch is
rejected with a :class:`ConflictError` instead of silently overwriting.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from pydantic import ValidationError

from .models import PROTECTED_FIELDS, Edge, Node, ProjectState, Requirement

# Moving things around is last-writer-wins; everything else is conflict-checked.
NO_CONFLICT_FIELDS = {"position"}


class OpError(ValueError):
    pass


class ConflictError(Exception):
    def __init__(self, conflicts: list[dict[str, Any]]):
        super().__init__(f"{len(conflicts)} conflicting change(s)")
        self.conflicts = conflicts


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "item"


def new_node_id(state: ProjectState, name: str) -> str:
    base = slugify(name)
    candidate = base
    while candidate in state.nodes:
        candidate = f"{base}-{secrets.token_hex(2)}"
    return candidate


def new_edge_id(state: ProjectState) -> str:
    while True:
        candidate = f"e-{secrets.token_hex(4)}"
        if candidate not in state.edges:
            return candidate


def next_requirement_id(state: ProjectState) -> str:
    nums = [int(m.group(1)) for rid in state.requirements if (m := re.match(r"REQ-(\d+)$", rid))]
    return f"REQ-{(max(nums) if nums else 0) + 1:03d}"


def resolve_node(state: ProjectState, ref: str | None) -> Node | None:
    """Find a node by id, or by case-insensitive name (convenient for AI and MCP callers)."""
    if not ref:
        return None
    if ref in state.nodes:
        return state.nodes[ref]
    wanted = ref.strip().lower()
    for node in state.nodes.values():
        if node.name.lower() == wanted:
            return node
    slug = slugify(ref)
    for node in state.nodes.values():
        if slugify(node.name) == slug:
            return node
    return None


def resolve_requirement(state: ProjectState, ref: str | None) -> Requirement | None:
    if not ref:
        return None
    if ref in state.requirements:
        return state.requirements[ref]
    wanted = ref.strip().lower()
    for req in state.requirements.values():
        if req.id.lower() == wanted or req.title.lower() == wanted:
            return req
    return None


def _auto_position(state: ProjectState) -> dict[str, float]:
    if not state.nodes:
        return {"x": 0, "y": 0}
    xs = [n.position.x for n in state.nodes.values()]
    ys = [n.position.y for n in state.nodes.values()]
    return {"x": (min(xs) + max(xs)) / 2, "y": max(ys) + 160}


def _apply_changes(entity, changes: dict[str, Any], base_rev: int | None, new_rev: int,
                   kind: str, conflicts: list[dict[str, Any]]):
    """Validate and apply field changes to a copy of ``entity``; record conflicts."""
    data = entity.model_dump()
    changed_fields = []
    for field, value in changes.items():
        if field in PROTECTED_FIELDS:
            continue
        if field not in data:
            raise OpError(f"unknown field '{field}' on {kind}")
        if data[field] == value:
            continue
        last = entity.field_revs.get(field, 0)
        if base_rev is not None and last > base_rev and field not in NO_CONFLICT_FIELDS:
            conflicts.append({
                "kind": kind, "id": entity.id, "name": getattr(entity, "name", None) or getattr(entity, "title", None),
                "field": field, "theirs": data[field], "yours": value,
                "base_rev": base_rev, "changed_at_rev": last,
            })
            continue
        data[field] = value
        changed_fields.append(field)
    try:
        updated = type(entity).model_validate(data)
    except ValidationError as exc:
        raise OpError(f"invalid {kind} update: {exc.errors()[0]['msg']}") from exc
    for field in changed_fields:
        updated.field_revs[field] = new_rev
    if changed_fields:
        updated.rev = new_rev
    return updated, changed_fields


def apply_ops(state: ProjectState, ops: list[dict[str, Any]]) -> tuple[ProjectState, list[str]]:
    """Apply a batch atomically. Returns the new state and human-readable change summaries."""
    if not ops:
        return state, []
    st = state.model_copy(deep=True)
    new_rev = st.rev + 1
    conflicts: list[dict[str, Any]] = []
    summary: list[str] = []

    for op in ops:
        kind = op.get("op")
        if kind == "add_node":
            raw = dict(op.get("node") or {})
            if not raw.get("name"):
                raise OpError("add_node requires a name")
            if raw.get("id") in st.nodes:
                raise OpError(f"node '{raw['id']}' already exists")
            if not raw.get("id"):
                raw["id"] = new_node_id(st, raw["name"])
            if not raw.get("position"):
                raw["position"] = _auto_position(st)
            raw["rev"] = new_rev
            try:
                node = Node.model_validate(raw)
            except ValidationError as exc:
                raise OpError(f"invalid node: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}") from exc
            node.field_revs = {f: new_rev for f in Node.model_fields if f not in PROTECTED_FIELDS}
            st.nodes[node.id] = node
            summary.append(f"added {node.type} '{node.name}'")

        elif kind == "update_node":
            node = resolve_node(st, op.get("id"))
            if not node:
                raise OpError(f"node '{op.get('id')}' not found")
            updated, fields = _apply_changes(node, op.get("changes") or {}, op.get("base_rev"), new_rev, "node", conflicts)
            st.nodes[node.id] = updated
            if fields and fields != ["position"]:
                summary.append(f"updated '{updated.name}' ({', '.join(fields)})")
            elif fields:
                summary.append(f"moved '{updated.name}'")

        elif kind == "remove_node":
            node = resolve_node(st, op.get("id"))
            if not node:
                raise OpError(f"node '{op.get('id')}' not found")
            del st.nodes[node.id]
            dropped = [eid for eid, e in st.edges.items() if node.id in (e.source, e.target)]
            for eid in dropped:
                del st.edges[eid]
            summary.append(f"removed '{node.name}'" + (f" and {len(dropped)} connection(s)" if dropped else ""))

        elif kind == "add_edge":
            raw = dict(op.get("edge") or {})
            src, tgt = resolve_node(st, raw.get("source")), resolve_node(st, raw.get("target"))
            if not src or not tgt:
                raise OpError(f"cannot connect '{raw.get('source')}' → '{raw.get('target')}': node not found")
            if any(e.source == src.id and e.target == tgt.id for e in st.edges.values()):
                continue  # idempotent
            raw.update(source=src.id, target=tgt.id, rev=new_rev)
            if not raw.get("id") or raw["id"] in st.edges:
                raw["id"] = new_edge_id(st)
            edge = Edge.model_validate(raw)
            edge.field_revs = {f: new_rev for f in Edge.model_fields if f not in PROTECTED_FIELDS}
            st.edges[edge.id] = edge
            summary.append(f"connected '{src.name}' → '{tgt.name}'" + (f" ({edge.protocol})" if edge.protocol else ""))

        elif kind == "update_edge":
            edge = st.edges.get(op.get("id", ""))
            if not edge:
                raise OpError(f"edge '{op.get('id')}' not found")
            changes = dict(op.get("changes") or {})
            for end in ("source", "target"):
                if end in changes:
                    n = resolve_node(st, changes[end])
                    if not n:
                        raise OpError(f"node '{changes[end]}' not found")
                    changes[end] = n.id
            updated, fields = _apply_changes(edge, changes, op.get("base_rev"), new_rev, "edge", conflicts)
            st.edges[edge.id] = updated
            if fields:
                summary.append(f"updated connection {edge.id} ({', '.join(fields)})")

        elif kind == "remove_edge":
            if op.get("id"):
                ids = [op["id"]] if op["id"] in st.edges else []
            else:
                src, tgt = resolve_node(st, op.get("source")), resolve_node(st, op.get("target"))
                ids = [eid for eid, e in st.edges.items()
                       if src and tgt and e.source == src.id and e.target == tgt.id]
            if not ids:
                raise OpError("connection not found")
            for eid in ids:
                e = st.edges.pop(eid)
                summary.append(f"disconnected '{st.nodes[e.source].name}' → '{st.nodes[e.target].name}'")

        elif kind == "add_requirement":
            raw = dict(op.get("requirement") or {})
            if not raw.get("title"):
                raise OpError("add_requirement requires a title")
            if not raw.get("id") or raw["id"] in st.requirements:
                raw["id"] = next_requirement_id(st)
            raw["rev"] = new_rev
            try:
                req = Requirement.model_validate(raw)
            except ValidationError as exc:
                raise OpError(f"invalid requirement: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}") from exc
            req.field_revs = {f: new_rev for f in Requirement.model_fields if f not in PROTECTED_FIELDS}
            st.requirements[req.id] = req
            summary.append(f"added {req.id} '{req.title}'")

        elif kind == "update_requirement":
            req = resolve_requirement(st, op.get("id"))
            if not req:
                raise OpError(f"requirement '{op.get('id')}' not found")
            updated, fields = _apply_changes(req, op.get("changes") or {}, op.get("base_rev"), new_rev, "requirement", conflicts)
            st.requirements[req.id] = updated
            if fields:
                summary.append(f"updated {req.id} ({', '.join(fields)})")

        elif kind == "remove_requirement":
            req = resolve_requirement(st, op.get("id"))
            if not req:
                raise OpError(f"requirement '{op.get('id')}' not found")
            del st.requirements[req.id]
            for node in st.nodes.values():
                if req.id in node.requirement_ids:
                    node.requirement_ids = [r for r in node.requirement_ids if r != req.id]
                    node.field_revs["requirement_ids"] = new_rev
                    node.rev = new_rev
            summary.append(f"removed {req.id} '{req.title}'")

        else:
            raise OpError(f"unknown op '{kind}'")

    if conflicts:
        raise ConflictError(conflicts)
    st.rev = new_rev
    return st, summary
