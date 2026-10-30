/**
 * The ArchFlow project model as a Yjs document — shared by the collab server and the web client.
 *
 * Layout: three top-level maps, `nodes`, `edges` and `requirements`, each keyed by entity id. Every entity
 * is its own Y.Map of fields, so two people editing *different* fields of the same component both win,
 * and concurrent edits to the *same* field resolve deterministically (last writer wins).
 *
 * Mutations come in two shapes:
 *  - ops    (`applyOps`):   what the canvas UI does — add/update/remove nodes, edges, requirements.
 *                           Removing a requirement is a soft delete (`deleted: true`, links kept) undone by
 *                           `restore_requirement`; permanent removal is an admin API call (`purge_requirement`).
 *  - patches (`applyPatch`): what the API sends for server-side changes (AI, MCP, restore) — field-level sets/deletes.
 */
import * as Y from "yjs";

export const COLLECTIONS = ["nodes", "edges", "requirements"] as const;
export type Collection = (typeof COLLECTIONS)[number];
export type Entity = Record<string, unknown> & { id: string };
export type Patch = Partial<Record<Collection, { set?: Record<string, Record<string, unknown>>; del?: string[] }>>;
export type Op = Record<string, unknown> & { op: string };

/** Transaction origins, so observers can tell local, remote and server edits apart. */
export const ORIGIN_LOCAL = "archflow:local";
export const ORIGIN_SERVER = "archflow:server";

// parent_id: "" = top-level (HLD) diagram, else the id of the component whose internal (LLD) diagram holds it.
const NODE_DEFAULTS = { type: "service", technology: "", vendor: "", catalog_id: "", description: "", position: { x: 0, y: 0 }, requirement_ids: [], props: {}, parent_id: "" };
const EDGE_DEFAULTS = { protocol: "", label: "" };
const REQ_DEFAULTS = {
  description: "", kind: "functional", category: "", priority: "medium", actors: [], acceptance_criteria: [], status: "draft",
  source: {}, deleted: false,
};

export const coll = (doc: Y.Doc, c: Collection) => doc.getMap<Y.Map<unknown>>(c);

function equal(a: unknown, b: unknown): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

/** Set fields on an entity, creating it if needed. Only changed fields are written (keeps updates small). */
function setFields(map: Y.Map<Y.Map<unknown>>, id: string, fields: Record<string, unknown>) {
  let ent = map.get(id);
  if (!ent) {
    ent = new Y.Map<unknown>();
    ent.set("id", id);
    for (const [k, v] of Object.entries(fields)) if (k !== "id") ent.set(k, v);
    map.set(id, ent);
    return;
  }
  for (const [k, v] of Object.entries(fields)) {
    if (k === "id") continue;
    if (!equal(ent.get(k), v)) ent.set(k, v);
  }
}

export function readEntity(ent: Y.Map<unknown>): Entity {
  return ent.toJSON() as Entity;
}

/** Plain-JSON projection of the document, shaped like the API's ProjectState. */
export function docToState(doc: Y.Doc): { rev: number; nodes: Record<string, Entity>; edges: Record<string, Entity>; requirements: Record<string, Entity> } {
  const out = { rev: 0, nodes: {}, edges: {}, requirements: {} } as ReturnType<typeof docToState>;
  for (const c of COLLECTIONS) {
    coll(doc, c).forEach((ent, id) => { out[c][id] = { ...readEntity(ent), id }; });
  }
  return out;
}

export function applyPatch(doc: Y.Doc, patch: Patch, origin: unknown = ORIGIN_SERVER) {
  doc.transact(() => {
    for (const c of COLLECTIONS) {
      const p = patch[c];
      if (!p) continue;
      const map = coll(doc, c);
      for (const id of p.del ?? []) map.delete(id);
      for (const [id, fields] of Object.entries(p.set ?? {})) setFields(map, id, fields);
    }
  }, origin);
}

/** Replace the whole content with a state (used to seed a document the first time). */
export function stateToPatch(state: { nodes?: Record<string, Entity>; edges?: Record<string, Entity>; requirements?: Record<string, Entity> }): Patch {
  const strip = (e: Entity) => {
    const { rev: _r, field_revs: _f, ...rest } = e as Entity & { rev?: unknown; field_revs?: unknown };
    return rest;
  };
  const patch: Patch = {};
  for (const c of COLLECTIONS) {
    const items = state[c] ?? {};
    patch[c] = { set: Object.fromEntries(Object.entries(items).map(([id, e]) => [id, strip(e)])) };
  }
  return patch;
}

// ---------------------------------------------------------------------------- ops (UI edits)
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "item";
const rand = () => Math.random().toString(36).slice(2, 6);

function uniqueId(map: { has(key: string): boolean }, base: string): string {
  let id = base;
  while (map.has(id)) id = `${base}-${rand()}`;
  return id;
}

function nextReqId(map: Y.Map<Y.Map<unknown>>): string {
  let max = 0;
  map.forEach((_v, id) => { const m = /^REQ-(\d+)$/.exec(id); if (m) max = Math.max(max, Number(m[1])); });
  return `REQ-${String(max + 1).padStart(3, "0")}`;
}

function findNode(doc: Y.Doc, ref: unknown): string | null {
  if (typeof ref !== "string") return null;
  const nodes = coll(doc, "nodes");
  if (nodes.has(ref)) return ref;
  const want = ref.trim().toLowerCase();
  let hit: string | null = null;
  nodes.forEach((ent, id) => { if (!hit && String(ent.get("name") ?? "").toLowerCase() === want) hit = id; });
  return hit;
}

export class OpError extends Error {}

/** Ids of every node inside `id`'s internal diagram, recursively (not including `id`). */
export function descendantIds(parents: Map<string, string>, id: string): string[] {
  const kids = new Map<string, string[]>();
  parents.forEach((p, nid) => { if (p) kids.set(p, [...(kids.get(p) ?? []), nid]); });
  const out: string[] = [];
  const seen = new Set([id]);
  const stack = [...(kids.get(id) ?? [])];
  while (stack.length) {
    const nid = stack.pop()!;
    if (seen.has(nid)) continue;
    seen.add(nid);
    out.push(nid);
    stack.push(...(kids.get(nid) ?? []));
  }
  return out;
}

const nodeParents = (doc: Y.Doc) => {
  const m = new Map<string, string>();
  coll(doc, "nodes").forEach((n, id) => m.set(id, String(n.get("parent_id") ?? "")));
  return m;
};

/**
 * Check a batch before touching the document: Yjs transactions cannot be rolled back, so atomicity
 * comes from validating everything first (including references to components added earlier in the batch).
 * Mirrors backend/archflow/ops.py, including the diagram-level rules: a connection joins two elements of the
 * same diagram (same parent_id), and a parent must exist and cannot be placed inside itself.
 */
export function validateOps(doc: Y.Doc, ops: Op[]): void {
  // A simulation of the nodes and connections as the batch proceeds.
  const sim = new Map<string, { name: string; parent: string }>();
  coll(doc, "nodes").forEach((n, id) => sim.set(id, { name: String(n.get("name") ?? ""), parent: String(n.get("parent_id") ?? "") }));
  const links = new Map<string, [string, string]>();
  coll(doc, "edges").forEach((e, id) => links.set(id, [String(e.get("source")), String(e.get("target"))]));
  const resolve = (ref: unknown): string | null => {
    if (typeof ref !== "string") return null;
    if (sim.has(ref)) return ref;
    const want = ref.trim().toLowerCase();
    for (const [id, n] of sim) if (n.name.toLowerCase() === want) return id;
    return null;
  };
  const where = (parent: string) => (parent ? `the internals of '${sim.get(parent)?.name ?? parent}'` : "the top-level diagram");
  const sameDiagram = (s: string, t: string) => {
    const a = sim.get(s)!, b = sim.get(t)!;
    if (a.parent !== b.parent) {
      throw new OpError(`cannot connect '${a.name}' → '${b.name}': they are in different diagrams `
        + `(${where(a.parent)} vs ${where(b.parent)}). Connections must stay within one diagram level.`);
    }
  };
  const parents = () => new Map([...sim].map(([id, n]) => [id, n.parent] as [string, string]));
  let fresh = 0;
  for (const op of ops) {
    switch (op.op) {
      case "add_node": {
        const n = op.node as Record<string, unknown> | undefined;
        if (!n?.name) throw new OpError("add_node requires a name");
        let parent = "";
        if (n.parent_id) {
          parent = resolve(n.parent_id) ?? "";
          if (!parent) throw new OpError(`parent component '${String(n.parent_id)}' not found`);
        }
        const id = n.id && !sim.has(String(n.id)) ? String(n.id) : `\u0000new-${fresh++}`;
        sim.set(id, { name: String(n.name), parent });
        break;
      }
      case "update_node": {
        const id = resolve(op.id);
        if (!id) throw new OpError(`node '${String(op.id)}' not found`);
        if (!op.changes || typeof op.changes !== "object") throw new OpError("update_node requires changes");
        const ch = op.changes as Record<string, unknown>;
        const cur = sim.get(id)!;
        if ("parent_id" in ch) {
          let parent = "";
          if (ch.parent_id) {
            parent = resolve(ch.parent_id) ?? "";
            if (!parent) throw new OpError(`parent component '${String(ch.parent_id)}' not found`);
            if (parent === id || descendantIds(parents(), id).includes(parent)) throw new OpError("a component cannot be placed inside itself");
          }
          if (parent !== cur.parent && [...links.values()].some(([s, t]) => s === id || t === id)) {
            throw new OpError(`'${cur.name}' has connections; remove them before moving it to another diagram`);
          }
          cur.parent = parent;
        }
        if (typeof ch.name === "string") cur.name = ch.name;
        break;
      }
      case "remove_node": {
        const id = resolve(op.id);
        if (!id) break; // already gone (e.g. removed concurrently)
        const gone = new Set([id, ...descendantIds(parents(), id)]);
        gone.forEach((g) => sim.delete(g));
        for (const [eid, [s, t]] of [...links]) if (gone.has(s) || gone.has(t)) links.delete(eid);
        break;
      }
      case "add_edge": {
        const e = op.edge as Record<string, unknown> | undefined;
        const s = resolve(e?.source), t = resolve(e?.target);
        if (!e || !s || !t) throw new OpError("cannot connect: component not found");
        sameDiagram(s, t);
        links.set(e.id ? String(e.id) : `\u0000new-${fresh++}`, [s, t]);
        break;
      }
      case "update_edge": {
        const ends = links.get(String(op.id));
        if (!ends) throw new OpError(`edge '${String(op.id)}' not found`);
        const ch = (op.changes ?? {}) as Record<string, unknown>;
        const s = "source" in ch ? resolve(ch.source) : ends[0], t = "target" in ch ? resolve(ch.target) : ends[1];
        if (!s || !t) throw new OpError("cannot connect: component not found");
        sameDiagram(s, t);
        links.set(String(op.id), [s, t]);
        break;
      }
      case "remove_edge": {
        if (op.id) { links.delete(String(op.id)); break; }
        const s = resolve(op.source), t = resolve(op.target);
        for (const [eid, [a, b]] of [...links]) if (a === s && b === t) links.delete(eid);
        break;
      }
      case "add_requirement":
        if (!(op.requirement as Record<string, unknown> | undefined)?.title) throw new OpError("add_requirement requires a title");
        break;
      case "update_requirement":
        if (!coll(doc, "requirements").has(String(op.id))) throw new OpError(`requirement '${String(op.id)}' not found`);
        break;
      case "remove_requirement":
        break;
      case "restore_requirement":
        if (!coll(doc, "requirements").has(String(op.id))) throw new OpError(`requirement '${String(op.id)}' not found`);
        break;
      default:
        throw new OpError(`unknown op '${op.op}'`);
    }
  }
}

/** Apply UI operations atomically (validated first; one Yjs transaction = one undo step). */
export function applyOps(doc: Y.Doc, ops: Op[], origin: unknown = ORIGIN_LOCAL): string[] {
  validateOps(doc, ops);
  const summary: string[] = [];
  doc.transact(() => {
    const nodes = coll(doc, "nodes"), edges = coll(doc, "edges"), reqs = coll(doc, "requirements");
    for (const op of ops) {
      switch (op.op) {
        case "add_node": {
          const n = { ...(op.node as Record<string, unknown>) };
          if (!n.name) throw new OpError("add_node requires a name");
          const id = n.id && !nodes.has(String(n.id)) ? String(n.id) : uniqueId(nodes, slug(String(n.name)));
          const parent_id = n.parent_id ? findNode(doc, n.parent_id) ?? "" : "";
          setFields(nodes, id, { ...NODE_DEFAULTS, ...n, id, parent_id });
          summary.push(`added '${n.name}'`);
          break;
        }
        case "update_node": {
          const id = findNode(doc, op.id);
          if (!id) throw new OpError(`node '${String(op.id)}' not found`);
          const changes = { ...(op.changes as Record<string, unknown>) };
          if (changes.parent_id) changes.parent_id = findNode(doc, changes.parent_id) ?? changes.parent_id;
          setFields(nodes, id, changes);
          break;
        }
        case "remove_node": {
          const id = findNode(doc, op.id);
          if (!id) break;  // already gone (e.g. removed concurrently)
          // A component's internal (LLD) diagram goes with it.
          const gone = new Set([id, ...descendantIds(nodeParents(doc), id)]);
          edges.forEach((e, eid) => { if (gone.has(String(e.get("source"))) || gone.has(String(e.get("target")))) edges.delete(eid); });
          gone.forEach((g) => nodes.delete(g));
          summary.push(`removed '${id}'` + (gone.size > 1 ? ` with ${gone.size - 1} internal element(s)` : ""));
          break;
        }
        case "add_edge": {
          const e = op.edge as Record<string, unknown>;
          const s = findNode(doc, e.source), t = findNode(doc, e.target);
          if (!s || !t) throw new OpError("cannot connect: component not found");
          let exists = false;
          edges.forEach((x) => { if (x.get("source") === s && x.get("target") === t) exists = true; });
          if (exists) break;
          const id = e.id && !edges.has(String(e.id)) ? String(e.id) : uniqueId(edges, `e-${rand()}${rand()}`);
          setFields(edges, id, { ...EDGE_DEFAULTS, ...e, id, source: s, target: t });
          break;
        }
        case "update_edge": {
          const id = String(op.id);
          if (!edges.has(id)) throw new OpError(`edge '${id}' not found`);
          setFields(edges, id, op.changes as Record<string, unknown>);
          break;
        }
        case "remove_edge": {
          if (op.id) { edges.delete(String(op.id)); break; }
          const s = findNode(doc, op.source), t = findNode(doc, op.target);
          edges.forEach((x, eid) => { if (x.get("source") === s && x.get("target") === t) edges.delete(eid); });
          break;
        }
        case "add_requirement": {
          const r = { ...(op.requirement as Record<string, unknown>) };
          if (!r.title) throw new OpError("add_requirement requires a title");
          const id = r.id && !reqs.has(String(r.id)) ? String(r.id) : nextReqId(reqs);
          setFields(reqs, id, { ...REQ_DEFAULTS, ...r, id });
          summary.push(`added ${id}`);
          break;
        }
        case "update_requirement": {
          const id = String(op.id);
          if (!reqs.has(id)) throw new OpError(`requirement '${id}' not found`);
          setFields(reqs, id, op.changes as Record<string, unknown>);
          break;
        }
        case "remove_requirement":
        case "restore_requirement": {
          const id = String(op.id);
          if (!reqs.has(id)) break; // already gone (e.g. removed permanently)
          const deleted = op.op === "remove_requirement";
          setFields(reqs, id, { deleted });
          summary.push(`${deleted ? "removed" : "restored"} ${id}`);
          break;
        }
        default:
          throw new OpError(`unknown op '${op.op}'`);
      }
    }
  }, origin);
  return summary;
}
