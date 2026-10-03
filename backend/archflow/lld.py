"""Low-level design (C4 levels 3–4) for one high-level component, and the views derived from it.

Every HLD component can have its own internal diagram: nodes whose ``parent_id`` is the component's id.
This module

* generates that diagram — with the LLM when one is configured (structured JSON output), otherwise (or when
  the model fails) with a deterministic template: controller / service / repository components, API
  endpoints inferred from the component's name and linked requirements, tables for connected databases and
  domain events for connected queues;
* turns a generated design into ordinary operations (so it goes through ``ops.apply_ops`` like every other
  edit, and shows up live on the canvas);
* derives read-only views from the model: an OpenAPI-like endpoint list, ER data, and Mermaid
  ``sequenceDiagram`` / ``erDiagram`` sources.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import llm
from .models import LLD_TYPES, Node, ProjectState
from .ops import descendants, resolve_node, slugify

log = logging.getLogger("archflow.lld")

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
COL_X, ROW_Y = 280, 120
# Default layout column per element type (left → right: API surface, logic, events, data).
TYPE_COLUMN = {"endpoint": 0, "component": 1, "event": 3, "table": 4}
MAX_ELEMENTS = 40


class LLDError(ValueError):
    """The request cannot be served (unsupported component, design already exists, …)."""


# --------------------------------------------------------------------------- naming helpers
_ROLE_WORDS = {"service", "services", "svc", "api", "server", "backend", "app", "application", "microservice",
               "agent", "mcp", "db", "database", "store", "datastore", "the", "system", "module", "worker"}


def _words(name: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9]+", name)]


def _base_words(name: str) -> list[str]:
    words = [w for w in _words(name) if w.lower() not in _ROLE_WORDS]
    return words or _words(name)[:1] or ["item"]


def _camel(words: list[str]) -> str:
    return "".join(w[:1].upper() + w[1:] for w in words) or "Item"


def _singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("ses", "xes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _plural(word: str) -> str:
    w = word.lower()
    if w.endswith("s"):
        return w
    if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    if w.endswith(("x", "ch", "sh")):
        return w + "es"
    return w + "s"


def _plural_res(res: str, sep: str = "_") -> str:
    """'order_line' → 'order_lines' (or 'order-lines')."""
    parts = res.split("_")
    parts[-1] = _plural(parts[-1])
    return sep.join(parts)


def resource_of(node: Node) -> str:
    """Singular snake_case resource noun for a component: 'Order Service' → 'order', 'Orders DB' → 'order'."""
    words = [w.lower() for w in _base_words(node.name)]
    words[-1] = _singular(words[-1])
    return "_".join(words)


# --------------------------------------------------------------------------- model helpers
def children(state: ProjectState, node_id: str) -> list[Node]:
    return [n for n in state.nodes.values() if n.parent_id == node_id]


def neighbours(state: ProjectState, node: Node) -> tuple[list[tuple[Node, str]], list[tuple[Node, str]]]:
    """(outgoing, incoming) neighbours on the node's own diagram, with the connection protocol."""
    out, inc = [], []
    for e in state.edges.values():
        if e.source == node.id and e.target in state.nodes:
            out.append((state.nodes[e.target], e.protocol or e.label))
        elif e.target == node.id and e.source in state.nodes:
            inc.append((state.nodes[e.source], e.protocol or e.label))
    return out, inc


def parse_columns(text: str) -> list[dict[str, str]]:
    """'id uuid pk, customer_id uuid fk, total numeric' → [{name, type, key}]."""
    cols = []
    for part in (text or "").replace("\n", ",").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        key = next((b.upper() for b in bits[2:] if b.lower() in ("pk", "fk", "uk")), "")
        cols.append({"name": bits[0], "type": bits[1] if len(bits) > 1 else "text", "key": key})
    return cols


# --------------------------------------------------------------------------- offline template generator
# requirement keyword → (method, path suffix, summary); {r} = plural resource, {s} = singular
_REQ_ACTIONS: list[tuple[str, str, str, str]] = [
    (r"\b(search|lookup|look up|find|filter|query)\b", "GET", "/{r}/search", "Search {r}"),
    (r"\bcancel", "POST", "/{r}/{{id}}/cancel", "Cancel {s}"),
    (r"\bescalat", "POST", "/{r}/{{id}}/escalate", "Escalate {s} to a human"),
    (r"\b(status|track)", "GET", "/{r}/{{id}}/status", "Get the status of {s}"),
    (r"\b(pay|payment|checkout|charge)", "POST", "/{r}/{{id}}/payments", "Pay for {s}"),
    (r"\b(chat|message|conversation|reply)", "POST", "/{r}/{{id}}/messages", "Send a message"),
    (r"\b(notify|notification|email|alert)", "POST", "/{r}/{{id}}/notifications", "Send a notification"),
    (r"\b(upload|attach|file|document)", "POST", "/{r}/{{id}}/attachments", "Upload an attachment"),
    (r"\b(auth|login|log in|sign[ -]?in)", "POST", "/auth/login", "Authenticate a user"),
    (r"\b(report|export|analytics)", "GET", "/{r}/reports", "Get a report"),
]


def _endpoints(state: ProjectState, node: Node, res: str) -> list[dict[str, Any]]:
    r, s0 = _plural_res(res, "-"), res.replace("_", " ")
    s = f"{'an' if s0[:1] in 'aeiou' else 'a'} {s0}"  # with article
    if node.type == "agent":
        base = [("POST", "/conversations", "Start a conversation", []),
                ("POST", "/conversations/{id}/messages", "Send a message and get the agent's reply", []),
                ("GET", "/conversations/{id}", "Get a conversation with its messages", [])]
    else:
        base = [("GET", f"/{r}", f"List {r.replace('-', ' ')}", []),
                ("POST", f"/{r}", f"Create {s}", []),
                ("GET", f"/{r}/{{id}}", f"Get one {s0}", [])]
    seen = {(m, p) for m, p, _, _ in base}
    extra: list[tuple[str, str, str, list[str]]] = []
    for rid in node.requirement_ids:
        req = state.requirements.get(rid)
        if not req:
            continue
        text = f"{req.title} {req.description}".lower()
        for pattern, method, path, summary in _REQ_ACTIONS:
            if re.search(pattern, text):
                p = path.format(r=r if node.type != "agent" else "conversations", s=s)
                if (method, p) not in seen:
                    seen.add((method, p))
                    extra.append((method, p, f"{summary.format(r=r, s=s)} ({rid}: {req.title})", [rid]))
                break
        if len(extra) >= 2:
            break
    if not extra and node.type != "agent":
        extra.append(("PATCH", f"/{r}/{{id}}", f"Update {s}", []))
    return [{"method": m, "path": p, "description": d, "requirement_ids": rids} for m, p, d, rids in base + extra]


def _table_columns(state: ProjectState, node: Node, res: str) -> str:
    cols = ["id uuid pk"]
    actors: list[str] = []
    for rid in node.requirement_ids:
        req = state.requirements.get(rid)
        for a in (req.actors if req else []):
            if re.search(r"\b(ai|system|assistant|bot|agent)\b", a.lower()):
                continue  # software actors don't own rows
            col = slugify(_singular(a.split()[-1])).replace("-", "_")
            if col and col not in actors and col != res:
                actors.append(col)
    cols += [f"{a}_id uuid fk" for a in actors[:2]]
    cols += ["status text", "data jsonb", "created_at timestamptz", "updated_at timestamptz"]
    return ", ".join(cols)


def _el(key: str, name: str, type_: str, col: int, description: str = "", technology: str = "",
        props: dict[str, str] | None = None, requirement_ids: list[str] | None = None) -> dict[str, Any]:
    return {"key": key, "name": name, "type": type_, "col": col, "description": description,
            "technology": technology, "props": props or {}, "requirement_ids": requirement_ids or []}


def _service_design(state: ProjectState, node: Node) -> dict[str, Any]:
    res = resource_of(node)
    camel = _camel(_base_words(node.name))
    tech = node.technology
    out, inc = neighbours(state, node)
    els: list[dict[str, Any]] = []
    conns: list[dict[str, str]] = []

    els.append(_el("controller", f"{camel}Controller", "component", 1, "HTTP layer: validation, auth context, "
                   "mapping requests to the service", tech))
    els.append(_el("service", f"{camel}Service", "component", 2, "Business rules and orchestration",
                   tech, requirement_ids=list(node.requirement_ids)))
    conns.append({"from": "controller", "to": "service", "label": "calls"})

    for i, ep in enumerate(_endpoints(state, node, res)):
        key = f"ep{i}"
        els.append(_el(key, f"{ep['method']} {ep['path']}", "endpoint", 0, ep["description"],
                       props={"method": ep["method"], "path": ep["path"]}, requirement_ids=ep["requirement_ids"]))
        conns.append({"from": key, "to": "controller", "label": "handled by"})

    dbs = [n for n, _ in out + inc if n.type in ("database", "storage") and n.type != "note"]
    dbs = list({d.id: d for d in dbs}.values())
    if dbs:
        els.append(_el("repository", f"{camel}Repository", "component", 3, "Data access: queries and persistence",
                       tech))
        conns.append({"from": "service", "to": "repository", "label": "uses"})
        for i, db in enumerate(dbs[:3]):
            if any(c.type == "table" for c in children(state, db.id)):
                continue  # the database's own design already defines its tables (shown in this service's views)
            table = _plural_res(res) if len(dbs) == 1 else f"{_plural_res(res)}_{slugify(_base_words(db.name)[0])}"
            key = f"table{i}"
            els.append(_el(key, table.replace("-", "_"), "table", 4, f"Stored in {db.name}", db.technology,
                           props={"columns": _table_columns(state, node, res), "database": db.name}))
            conns.append({"from": "repository", "to": key, "label": "reads / writes"})

    queues_out = [n for n, _ in out if n.type == "queue"]
    queues_in = [n for n, _ in inc if n.type == "queue"]
    if queues_out:
        els.append(_el("publisher", f"{camel}EventPublisher", "component", 3, "Publishes domain events",
                       tech))
        conns.append({"from": "service", "to": "publisher", "label": "emits"})
        for verb in ("Created", "Updated"):
            key = f"event{verb}"
            els.append(_el(key, f"{_camel(res.split('_'))}{verb}", "event", 4,
                           f"Published to {queues_out[0].name} when a {res.replace('_', ' ')} is {verb.lower()}",
                           queues_out[0].technology, props={"channel": queues_out[0].name}))
            conns.append({"from": "publisher", "to": key, "label": "publishes"})
    if queues_in:
        els.append(_el("consumer", f"{camel}EventConsumer", "component", 1,
                       f"Consumes messages from {queues_in[0].name}", tech))
        conns.append({"from": "consumer", "to": "service", "label": "calls"})

    clients = [n for n, _ in out if n.type in ("service", "external", "llm", "mcp", "agent", "security", "cache")]
    for i, dep in enumerate(clients[:3]):
        key = f"client{i}"
        els.append(_el(key, f"{_camel(_base_words(dep.name))}Client", "component", 3,
                       f"Adapter for {dep.name}" + (f" ({dep.technology})" if dep.technology else ""), tech))
        conns.append({"from": "service", "to": key, "label": "uses"})
    return {"elements": els, "connections": conns}


def _database_design(state: ProjectState, node: Node) -> dict[str, Any]:
    out, inc = neighbours(state, node)
    resources: list[tuple[str, Node]] = [(resource_of(node), node)]
    for n, _ in inc + out:
        if n.type in ("service", "agent", "mcp", "gateway"):
            resources.append((resource_of(n), n))
    seen: set[str] = set()
    els, conns = [], []
    for res, owner in resources:
        table = _plural_res(res)
        if table in seen:
            continue
        seen.add(table)
        key = f"table{len(seen)}"
        desc = f"Owned by {owner.name}" if owner is not node else f"Core table of {node.name}"
        els.append(_el(key, table, "table", len(seen) - 1, desc, node.technology,
                       props={"columns": _table_columns(state, owner, res), "database": node.name}))
        if len(seen) >= 4:
            break
    first = els[0]["name"]
    child = f"{_singular(first)}_items"
    els.append(_el("items", child, "table", 0, f"Line items of {first}", node.technology,
                   props={"columns": f"id uuid pk, {_singular(first)}_id uuid fk, position int, data jsonb",
                          "database": node.name}))
    conns.append({"from": els[0]["key"], "to": "items", "label": "1:N"})
    els.append(_el("audit", "audit_log", "table", 1, "Who changed what, and when", node.technology,
                   props={"columns": "id uuid pk, entity text, entity_id uuid, action text, actor text, "
                                     "created_at timestamptz", "database": node.name}))
    return {"elements": els, "connections": conns}


def _queue_design(state: ProjectState, node: Node) -> dict[str, Any]:
    out, inc = neighbours(state, node)
    els, conns = [], []
    els.append(_el("topic", f"{_camel(_base_words(node.name))}Topic", "component", 1,
                   f"Topic / channel on {node.technology or node.name}", node.technology))
    for i, (prod, _) in enumerate(inc[:3]):
        res = resource_of(prod)
        for verb in ("Created", "Updated"):
            key = f"ev{i}{verb}"
            els.append(_el(key, f"{_camel(res.split('_'))}{verb}", "event", 0, f"Published by {prod.name}",
                           node.technology, props={"channel": node.name}))
            conns.append({"from": key, "to": "topic", "label": "published to"})
    if len(els) == 1:
        els.append(_el("ev", "DomainEvent", "event", 0, "Event published to this channel", node.technology,
                       props={"channel": node.name}))
        conns.append({"from": "ev", "to": "topic", "label": "published to"})
    for i, (cons, _) in enumerate(out[:3]):
        key = f"sub{i}"
        els.append(_el(key, f"{_camel(_base_words(cons.name))}Subscription", "component", 2,
                       f"Delivers events to {cons.name}", node.technology))
        conns.append({"from": "topic", "to": key, "label": "delivers"})
    return {"elements": els, "connections": conns}


def offline_design(state: ProjectState, node: Node) -> dict[str, Any]:
    if node.type == "note":
        raise LLDError("Notes have no internal design")
    if node.type in ("database", "storage"):
        return _database_design(state, node)
    if node.type == "queue":
        return _queue_design(state, node)
    return _service_design(state, node)


# --------------------------------------------------------------------------- LLM generator
_S = {"type": "string"}
LLD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["elements", "connections"],
    "properties": {
        "elements": {"type": "array", "minItems": 1, "maxItems": MAX_ELEMENTS, "items": {
            "type": "object", "required": ["key", "name", "type"],
            "properties": {
                "key": _S, "name": _S, "type": {"type": "string", "enum": list(LLD_TYPES)},
                "technology": _S, "description": _S,
                "method": {"type": "string", "enum": list(HTTP_METHODS)},
                "path": _S,
                "columns": {"type": "string", "description": "Tables only: comma-separated 'name type [pk|fk]'"},
                "requirement_ids": {"type": "array", "items": _S},
            }}},
        "connections": {"type": "array", "items": {
            "type": "object", "required": ["from", "to"],
            "properties": {"from": _S, "to": _S, "label": _S}}},
    },
}

LLD_SYSTEM = """You are a senior software architect producing the low-level design (C4 component level) of ONE \
component of a larger system. Return only JSON matching the schema.

Element types:
- component: an internal module or class group (controller, service, repository, client adapter, consumer…)
- endpoint: one API operation; set method and path (REST style, e.g. GET /orders/{id}); name = "METHOD /path"
- table: one database table owned by this component; set columns as "name type [pk|fk]" comma-separated
- event: one domain event it publishes or consumes (PascalCase, past tense, e.g. OrderPlaced)

Rules: 3-6 endpoints for an API-facing component; tables only for databases it actually uses; events only if \
it talks to a queue/stream; 6-20 elements in total. Connections go from caller to callee (endpoint → \
controller → service → repository → table; publisher → event). Use requirement_ids from the given \
requirements where an element implements one. Keys are short unique identifiers referenced by connections."""


def _llm_prompt(state: ProjectState, node: Node) -> str:
    out, inc = neighbours(state, node)
    reqs = [state.requirements[r].model_dump(include={"id", "title", "description", "acceptance_criteria"})
            for r in node.requirement_ids if r in state.requirements]
    payload = {
        "component": {"id": node.id, "name": node.name, "type": node.type, "technology": node.technology,
                      "description": node.description},
        "calls": [{"name": n.name, "type": n.type, "technology": n.technology, "protocol": p} for n, p in out],
        "called_by": [{"name": n.name, "type": n.type, "protocol": p} for n, p in inc],
        "requirements": reqs,
    }
    return f"Design the internals of this component:\n{json.dumps(payload, indent=1)}"


def normalize_design(raw: dict[str, Any], state: ProjectState) -> dict[str, Any]:
    """Clean a model-produced design; raises ValueError when nothing usable remains."""
    els: list[dict[str, Any]] = []
    keys: set[str] = set()
    for e in (raw.get("elements") or [])[:MAX_ELEMENTS]:
        if not isinstance(e, dict) or e.get("type") not in LLD_TYPES or not str(e.get("name") or "").strip():
            continue
        key = str(e.get("key") or e["name"]).strip()
        if key in keys:
            continue
        keys.add(key)
        props: dict[str, str] = {}
        name = str(e["name"]).strip()[:120]
        if e["type"] == "endpoint":
            method = str(e.get("method") or name.split()[0]).upper()
            method = method if method in HTTP_METHODS else "GET"
            path = str(e.get("path") or (name.split()[1] if len(name.split()) > 1 else "/"))
            path = path if path.startswith("/") else "/" + path
            props = {"method": method, "path": path}
            name = f"{method} {path}"
        elif e["type"] == "table":
            props = {"columns": str(e.get("columns") or "id uuid pk")}
        els.append(_el(key, name, e["type"], TYPE_COLUMN[e["type"]], str(e.get("description") or "")[:500],
                       str(e.get("technology") or "")[:80], props,
                       [r for r in e.get("requirement_ids") or [] if r in state.requirements]))
    if not els:
        raise ValueError("design has no valid elements")
    conns = [{"from": str(c["from"]), "to": str(c["to"]), "label": str(c.get("label") or "")[:60]}
             for c in raw.get("connections") or []
             if isinstance(c, dict) and c.get("from") in keys and c.get("to") in keys and c.get("from") != c.get("to")]
    return {"elements": els, "connections": conns}


def llm_design(state: ProjectState, node: Node, org_id: str | None = None) -> dict[str, Any]:
    data = llm.complete_json(LLD_SYSTEM, [{"role": "user", "content": _llm_prompt(state, node)}], LLD_SCHEMA,
                             group="reasoning", org_id=org_id)
    return normalize_design(data, state)


def generate_design(state: ProjectState, node: Node, org_id: str | None = None) -> tuple[dict[str, Any], str]:
    """(design, engine). Uses the LLM when available and falls back to the offline template."""
    if node.type == "note":
        raise LLDError("Notes have no internal design")
    if llm.available():
        try:
            return llm_design(state, node, org_id), "llm"
        except (llm.LLMUnavailable, llm.LLMRefused, ValueError, KeyError, TypeError) as exc:
            log.info("LLM low-level design failed (%s); using the template", type(exc).__name__)
    return offline_design(state, node), "offline"


# --------------------------------------------------------------------------- design → operations
def _layout(elements: list[dict[str, Any]]) -> None:
    cols: dict[int, list[dict[str, Any]]] = {}
    for e in elements:
        cols.setdefault(int(e.get("col", TYPE_COLUMN.get(e["type"], 1))), []).append(e)
    for col, items in cols.items():
        height = (len(items) - 1) * ROW_Y
        for i, e in enumerate(items):
            e["position"] = {"x": col * COL_X, "y": round(i * ROW_Y - height / 2)}


def design_ops(state: ProjectState, node: Node, design: dict[str, Any], replace: bool = False) -> list[dict[str, Any]]:
    """Operations that create ``design`` inside ``node``'s diagram (optionally replacing what is there)."""
    existing = children(state, node.id)
    if existing and not replace:
        raise LLDError(f"'{node.name}' already has a low-level design ({len(existing)} elements); "
                       "pass replace=true to regenerate it")
    ops: list[dict[str, Any]] = [{"op": "remove_node", "id": c.id} for c in existing]
    freed = set(descendants(state, node.id))
    taken = set(state.nodes) - freed
    elements = [dict(e) for e in design["elements"]]
    _layout(elements)
    ids: dict[str, str] = {}
    for e in elements:
        base = f"{node.id}--{slugify(e['name'])}"[:70]
        nid, n = base, 2
        while nid in taken:
            nid, n = f"{base}-{n}", n + 1
        taken.add(nid)
        ids[e["key"]] = nid
        ops.append({"op": "add_node", "node": {
            "id": nid, "name": e["name"], "type": e["type"], "technology": e.get("technology", ""),
            "description": e.get("description", ""), "props": e.get("props") or {}, "parent_id": node.id,
            "requirement_ids": e.get("requirement_ids") or [], "position": e["position"]}})
    seen: set[tuple[str, str]] = set()
    for c in design.get("connections") or []:
        a, b = ids.get(c["from"]), ids.get(c["to"])
        if a and b and a != b and (a, b) not in seen:
            seen.add((a, b))
            ops.append({"op": "add_edge", "edge": {"source": a, "target": b, "label": c.get("label", "")}})
    return ops


def build(state: ProjectState, node_ref: str, replace: bool = False,
          org_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Everything needed to generate one component's LLD: (ops, info). Raises LLDError / KeyError."""
    node = resolve_node(state, node_ref)
    if not node:
        raise KeyError(node_ref)
    if children(state, node.id) and not replace:
        raise LLDError(f"'{node.name}' already has a low-level design; pass replace=true to regenerate it")
    design, engine = generate_design(state, node, org_id)
    ops = design_ops(state, node, design, replace)
    counts: dict[str, int] = {}
    for e in design["elements"]:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    return ops, {"node_id": node.id, "name": node.name, "engine": engine, "counts": counts,
                 "connections": sum(1 for o in ops if o["op"] == "add_edge")}


# --------------------------------------------------------------------------- views
def _mid(name: str, used: dict[str, str]) -> str:
    """A Mermaid-safe participant/entity id."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "x"
    if base[0].isdigit():
        base = "n_" + base
    out, i = base, 2
    while out in used.values():
        out, i = f"{base}_{i}", i + 1
    return out


def _q(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def api_view(state: ProjectState, node: Node) -> list[dict[str, Any]]:
    eps = [c for c in children(state, node.id) if c.type == "endpoint"]
    rows = []
    for e in eps:
        method = (e.props.get("method") or e.name.split()[0]).upper()
        path = e.props.get("path") or (e.name.split()[1] if len(e.name.split()) > 1 else "/")
        rows.append({"id": e.id, "method": method, "path": path, "description": e.description,
                     "requirement_ids": e.requirement_ids})
    order = {m: i for i, m in enumerate(HTTP_METHODS)}
    return sorted(rows, key=lambda r: (r["path"].count("/"), r["path"], order.get(r["method"], 9)))


def openapi_text(node: Node, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = ["openapi: 3.1.0", "info:", f"  title: {node.name} API", "  version: 0.1.0", "paths:"]
    by_path: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_path.setdefault(r["path"], []).append(r)
    for path, ops in by_path.items():
        lines.append(f"  {path}:")
        for r in ops:
            lines.append(f"    {r['method'].lower()}:")
            lines.append(f"      summary: {json.dumps(r['description'] or r['method'] + ' ' + path)}")
            if r["requirement_ids"]:
                lines.append(f"      x-requirements: [{', '.join(r['requirement_ids'])}]")
            lines.append("      responses:")
            lines.append(f"        \"{'201' if r['method'] == 'POST' else '200'}\":")
            lines.append("          description: OK")
    return "\n".join(lines)


def _tables_for(state: ProjectState, node: Node) -> list[Node]:
    tables = [c for c in children(state, node.id) if c.type == "table"]
    if tables:
        return tables
    # A service without its own tables: show the tables of the databases it uses.
    out, inc = neighbours(state, node)
    for db, _ in out + inc:
        if db.type in ("database", "storage"):
            tables += [c for c in children(state, db.id) if c.type == "table"]
    return tables


def er_view(state: ProjectState, node: Node) -> dict[str, Any]:
    tables = _tables_for(state, node)
    ids = {t.id for t in tables}
    rels = [{"from": e.source, "to": e.target, "label": e.label or e.protocol}
            for e in state.edges.values() if e.source in ids and e.target in ids]
    return {"tables": [{"id": t.id, "name": t.name, "description": t.description,
                        "columns": parse_columns(t.props.get("columns", ""))} for t in tables],
            "relations": rels}


def er_mermaid(er: dict[str, Any]) -> str:
    if not er["tables"]:
        return ""
    used: dict[str, str] = {}
    lines = ["erDiagram"]
    for t in er["tables"]:
        used[t["id"]] = _mid(t["name"], used)
        lines.append(f"    {used[t['id']]} {{")
        for c in t["columns"]:
            typ = re.sub(r"[^A-Za-z0-9_]", "_", c["type"]) or "text"
            name = re.sub(r"[^A-Za-z0-9_]", "_", c["name"]) or "col"
            lines.append(f"        {typ} {name}" + (f" {c['key']}" if c["key"] in ("PK", "FK", "UK") else ""))
        lines.append("    }")
    for r in er["relations"]:
        lines.append(f"    {used[r['from']]} ||--o{{ {used[r['to']]} : \"{_q(r['label'] or 'has')}\"")
    return "\n".join(lines)


def _caller_chain(state: ProjectState, node: Node) -> list[Node]:
    """Shortest path of callers from a client (or any entry point) down to ``node`` on its own diagram."""
    incoming: dict[str, list[str]] = {}
    for e in state.edges.values():
        if e.source in state.nodes and e.target in state.nodes:
            incoming.setdefault(e.target, []).append(e.source)
    prev: dict[str, str] = {}
    frontier, seen = [node.id], {node.id}
    found: str | None = None
    while frontier and not found:
        nxt = []
        for nid in frontier:
            for src in incoming.get(nid, []):
                if src in seen:
                    continue
                seen.add(src)
                prev[src] = nid
                if state.nodes[src].type == "client":
                    found = src
                    break
                nxt.append(src)
            if found:
                break
        frontier = nxt
    if not found:
        # no client upstream: start from the direct caller (if any)
        direct = incoming.get(node.id, [])
        return [state.nodes[direct[0]], node] if direct else [node]
    chain = [found]
    while chain[-1] != node.id:
        chain.append(prev[chain[-1]])
    return [state.nodes[n] for n in chain]


def sequence_mermaid(state: ProjectState, node: Node, rows: list[dict[str, Any]]) -> str:
    chain = _caller_chain(state, node)
    out, _ = neighbours(state, node)
    deps = [(n, p) for n, p in out if n.type != "note" and n.id not in {c.id for c in chain}]
    used: dict[str, str] = {}
    lines = ["sequenceDiagram", "    autonumber"]
    for n in chain + [d for d, _ in deps]:
        if n.id in used:
            continue
        used[n.id] = _mid(n.name, used)
        kind = "actor" if n.type == "client" else "participant"
        lines.append(f"    {kind} {used[n.id]} as {_q(n.name)}")
    main = next((r for r in rows if r["method"] == "POST"), rows[0] if rows else None)
    request = f"{main['method']} {main['path']}" if main else "request"
    proto = {(e.source, e.target): e.protocol or e.label for e in state.edges.values()}
    for a, b in zip(chain, chain[1:]):
        p = proto.get((a.id, b.id), "")
        msg = f"{p} {request}" if b.id == node.id else (p or "request")  # the endpoint is this component's API
        lines.append(f"    {used[a.id]}->>{used[b.id]}: {_q(msg.strip())}")
    if len(chain) > 1:
        lines.append(f"    activate {used[node.id]}")
    for dep, p in deps:
        label = (p + " ").lstrip() if p else ""
        if dep.type == "queue":
            ev = next((c.name for c in children(state, node.id) if c.type == "event"), "event")
            lines.append(f"    {used[node.id]}-){used[dep.id]}: {_q(label + 'publish ' + ev)}")
        else:
            verb = {"database": "query / write", "storage": "read / write", "cache": "get / set",
                    "llm": "prompt", "mcp": "tool call", "security": "verify token"}.get(dep.type, "call")
            lines.append(f"    {used[node.id]}->>{used[dep.id]}: {_q(label + verb)}")
            lines.append(f"    {used[dep.id]}-->>{used[node.id]}: result")
    if len(chain) > 1:
        lines.append(f"    deactivate {used[node.id]}")
    for a, b in reversed(list(zip(chain, chain[1:]))):
        status = "201 Created" if main and main["method"] == "POST" else "200 OK"
        lines.append(f"    {used[b.id]}-->>{used[a.id]}: {status}")
    return "\n".join(lines)


def views(state: ProjectState, node: Node) -> dict[str, Any]:
    rows = api_view(state, node)
    er = er_view(state, node)
    return {
        "node_id": node.id, "name": node.name,
        "has_lld": bool(children(state, node.id)),
        "api": {"endpoints": rows, "openapi": openapi_text(node, rows)},
        "er": {**er, "mermaid": er_mermaid(er)},
        "sequence": {"mermaid": sequence_mermaid(state, node, rows)},
    }
