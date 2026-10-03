/**
 * The ArchFlow project model as a Yjs document — shared by the collab server and the web client.
 *
 * Layout: three top-level maps, `nodes`, `edges` and `requirements`, each keyed by entity id. Every entity
 * is its own Y.Map of fields, so two people editing *different* fields of the same component both win,
 * and concurrent edits to the *same* field resolve deterministically (last writer wins).
 *
 * Mutations come in two shapes:
 *  - ops    (`applyOps`):   what the canvas UI does — add/update/remove nodes, edges, requirements.
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

const NODE_DEFAULTS = { type: "service", technology: "", vendor: "", catalog_id: "", description: "", position: { x: 0, y: 0 }, requirement_ids: [], props: {} };
const EDGE_DEFAULTS = { protocol: "", label: "" };
const REQ_DEFAULTS = {
  description: "", kind: "functional", category: "", priority: "medium", actors: [], acceptance_criteria: [], status: "draft",
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

/**
 * Check a batch before touching the document: Yjs transactions cannot be rolled back, so atomicity
 * comes from validating everything first (including references to components added earlier in the batch).
 */
export function validateOps(doc: Y.Doc, ops: Op[]): void {
  const names = new Set<string>();
  coll(doc, "nodes").forEach((n, id) => { names.add(id); names.add(String(n.get("name") ?? "").toLowerCase()); });
  const has = (ref: unknown) => typeof ref === "string" && (names.has(ref) || names.has(ref.trim().toLowerCase()));
  for (const op of ops) {
    switch (op.op) {
      case "add_node": {
        const n = op.node as Record<string, unknown> | undefined;
        if (!n?.name) throw new OpError("add_node requires a name");
        if (n.id) names.add(String(n.id));
        names.add(String(n.name).toLowerCase());
        break;
      }
      case "update_node":
        if (!has(op.id)) throw new OpError(`node '${String(op.id)}' not found`);
        if (!op.changes || typeof op.changes !== "object") throw new OpError("update_node requires changes");
        break;
      case "add_edge": {
        const e = op.edge as Record<string, unknown> | undefined;
        if (!e || !has(e.source) || !has(e.target)) throw new OpError("cannot connect: component not found");
        break;
      }
      case "update_edge":
        if (!coll(doc, "edges").has(String(op.id))) throw new OpError(`edge '${String(op.id)}' not found`);
        break;
      case "add_requirement":
        if (!(op.requirement as Record<string, unknown> | undefined)?.title) throw new OpError("add_requirement requires a title");
        break;
      case "update_requirement":
        if (!coll(doc, "requirements").has(String(op.id))) throw new OpError(`requirement '${String(op.id)}' not found`);
        break;
      case "remove_node": case "remove_edge": case "remove_requirement":
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
          setFields(nodes, id, { ...NODE_DEFAULTS, ...n, id });
          summary.push(`added '${n.name}'`);
          break;
        }
        case "update_node": {
          const id = findNode(doc, op.id);
          if (!id) throw new OpError(`node '${String(op.id)}' not found`);
          setFields(nodes, id, op.changes as Record<string, unknown>);
          break;
        }
        case "remove_node": {
          const id = findNode(doc, op.id);
          if (!id) break;  // already gone (e.g. removed concurrently)
          edges.forEach((e, eid) => { if (e.get("source") === id || e.get("target") === id) edges.delete(eid); });
          nodes.delete(id);
          summary.push(`removed '${id}'`);
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
        case "remove_requirement": {
          const id = String(op.id);
          reqs.delete(id);
          nodes.forEach((n) => {
            const ids = (n.get("requirement_ids") as string[] | undefined) ?? [];
            if (ids.includes(id)) n.set("requirement_ids", ids.filter((x) => x !== id));
          });
          break;
        }
        default:
          throw new OpError(`unknown op '${op.op}'`);
      }
    }
  }, origin);
  return summary;
}
