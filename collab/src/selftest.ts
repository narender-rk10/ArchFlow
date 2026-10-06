/**
 * End-to-end check against a running API (:8000) and collab server (:1234):
 * two "browsers" edit the same canvas concurrently, an MCP-style API edit lands in the live document,
 * and the API ends up with the merged state attributed to the editors.
 *
 *   npm test        (with both servers running)
 */
import { HocuspocusProvider } from "@hocuspocus/provider";
import * as Y from "yjs";
import { applyOps, coll, docToState } from "./ydoc.js";

const API = process.env.ARCHFLOW_API ?? "http://localhost:8000";
const WS = process.env.COLLAB_WS ?? "ws://localhost:1234";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
function check(label: string, ok: boolean, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${!ok && detail ? ` — ${detail}` : ""}`);
  if (!ok) failures++;
}
async function waitFor(fn: () => boolean, ms = 4000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (fn()) return true; await sleep(50); }
  return fn();
}
// ---- accounts: two throwaway users (Alice owns a fresh workspace and invites Bob as a developer) ----
// Requests carry the trusted dev Origin (CSRF check) and the session cookie returned at sign-up.
const ORIGIN = process.env.ARCHFLOW_ORIGIN ?? "http://localhost:5173";
type Session = { cookie: string; name: string };
async function call(s: Session | null, method: string, path: string, body?: unknown) {
  const r = await fetch(`${API}${path}`, {
    method, headers: { Origin: ORIGIN, ...(body ? { "Content-Type": "application/json" } : {}), ...(s ? { Cookie: s.cookie } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok && r.status !== 204) throw new Error(`${method} ${path}: ${r.status} ${await r.text()}`);
  return r;
}
async function signup(name: string, invite?: string): Promise<Session & { orgs: { id: string }[] }> {
  const email = `selftest-${name.toLowerCase()}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
  const password = `pw-${Math.random().toString(36).slice(2)}-${Date.now()}`; // throwaway, never stored
  const r = await call(null, "POST", "/api/auth/signup", { email, name, password, invite });
  const cookie = (r.headers.get("set-cookie") ?? "").split(";")[0];
  return { cookie, name, orgs: (await r.json()).orgs };
}
const alice = await signup("Alice");
const orgId = alice.orgs[0].id;
const invite = await (await call(alice, "POST", `/api/orgs/${orgId}/invites`, { role: "developer" })).json();
const bob = await signup("Bob", invite.token);

function client(s: Session, doc: string, pid: string, branch = "main") {
  const ydoc = new Y.Doc();
  const token = async () =>
    (await (await call(s, "GET", `/api/projects/${pid}/collab-ticket?branch=${encodeURIComponent(branch)}&color=%230af`)).json()).ticket;
  const provider = new HocuspocusProvider({ url: WS, name: doc, document: ydoc, token });
  const synced = new Promise<void>((res) => provider.on("synced", () => res()));
  return { ydoc, provider, synced };
}
const node = (d: Y.Doc, id: string) => coll(d, "nodes").get(id)?.toJSON() as Record<string, unknown> | undefined;

const project = await (await call(alice, "POST", "/api/projects",
  { name: "collab-selftest", template: "ai-customer-support", org_id: orgId })).json();
const docName = `${project.id}:main`;

try {
  const a = client(alice, docName, project.id), b = client(bob, docName, project.id);
  await Promise.all([a.synced, b.synced]);
  check("both clients load the seeded architecture", Object.keys(docToState(b.ydoc).nodes).length === 10);

  applyOps(a.ydoc, [{ op: "add_node", node: { id: "kafka", name: "Kafka", type: "queue", technology: "Kafka", position: { x: 400, y: 500 } } },
                    { op: "add_edge", edge: { source: "order-service", target: "kafka", protocol: "events" } }]);
  check("Bob sees Alice's new component live", await waitFor(() => !!node(b.ydoc, "kafka")));

  // Concurrent edits to different fields of the same component both survive.
  applyOps(a.ydoc, [{ op: "update_node", id: "orders-db", changes: { name: "Orders Store" } }]);
  applyOps(b.ydoc, [{ op: "update_node", id: "orders-db", changes: { technology: "CockroachDB" } }]);
  const merged = await waitFor(() => node(a.ydoc, "orders-db")?.technology === "CockroachDB" && node(b.ydoc, "orders-db")?.name === "Orders Store");
  check("concurrent edits to different fields merge", merged, JSON.stringify(node(a.ydoc, "orders-db")));

  // Concurrent edits to the same field converge to one value on both sides.
  applyOps(a.ydoc, [{ op: "update_node", id: "llm", changes: { technology: "gemini" } }]);
  applyOps(b.ydoc, [{ op: "update_node", id: "llm", changes: { technology: "claude" } }]);
  await sleep(600);
  check("concurrent edits to the same field converge", node(a.ydoc, "llm")?.technology === node(b.ydoc, "llm")?.technology);

  // The API receives the merged projection after the store debounce, attributed to both editors.
  const apiHas = async () => {
    const s = (await (await call(alice, "GET", `/api/projects/${project.id}`)).json()).state;
    return s.nodes.kafka && s.nodes["orders-db"].technology === "CockroachDB" && s.nodes["orders-db"].name === "Orders Store";
  };
  let ok = false;
  for (let i = 0; i < 40 && !ok; i++) { ok = await apiHas(); if (!ok) await sleep(250); }
  check("API state receives the merged document", ok);
  const activity = await (await call(alice, "GET", `/api/projects/${project.id}/activity`)).json();
  check("activity is attributed to the editors", activity.some((x: { actor: string }) => /Alice/.test(x.actor) && /Bob/.test(x.actor)),
    JSON.stringify(activity.slice(0, 2)));

  // A server-side edit (what an MCP agent or the AI does) appears in the live document, merged.
  applyOps(b.ydoc, [{ op: "update_node", id: "helpdesk", changes: { description: "edited by Bob" } }]);
  await call(bob, "POST", `/api/projects/${project.id}/ops`,
    { ops: [{ op: "update_node", id: "helpdesk", changes: { technology: "Freshdesk" } }] });
  const viaMcp = await waitFor(() => node(a.ydoc, "helpdesk")?.technology === "Freshdesk");
  check("MCP/API edit lands in the live document", viaMcp);
  check("…without overwriting a concurrent human edit", node(a.ydoc, "helpdesk")?.description === "edited by Bob",
    JSON.stringify(node(a.ydoc, "helpdesk")));

  // AI command (offline parser when no LLM key) also flows into the live doc.
  await call(alice, "POST", `/api/projects/${project.id}/ai/command`,
    { text: "Add Redis between Order Service and Orders Store" });
  check("AI command appears on everyone's canvas", await waitFor(() => !!node(b.ydoc, "redis")));

  a.provider.destroy(); b.provider.destroy();
} finally {
  await sleep(300);
  await call(alice, "DELETE", `/api/projects/${project.id}`);
}
console.log(failures ? `\n${failures} check(s) failed` : "\nAll collaboration checks passed");
process.exit(failures ? 1 : 0);
