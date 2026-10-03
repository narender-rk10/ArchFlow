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
function client(name: string, doc: string) {
  const ydoc = new Y.Doc();
  const provider = new HocuspocusProvider({ url: WS, name: doc, document: ydoc, token: JSON.stringify({ name, color: "#0af" }) });
  const synced = new Promise<void>((res) => provider.on("synced", () => res()));
  return { ydoc, provider, synced };
}
const node = (d: Y.Doc, id: string) => coll(d, "nodes").get(id)?.toJSON() as Record<string, unknown> | undefined;

const project = await (await fetch(`${API}/api/projects`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ name: "collab-selftest", template: "ai-customer-support" }),
})).json();
const docName = `${project.id}:main`;

try {
  const a = client("Alice", docName), b = client("Bob", docName);
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
    const s = (await (await fetch(`${API}/api/projects/${project.id}`)).json()).state;
    return s.nodes.kafka && s.nodes["orders-db"].technology === "CockroachDB" && s.nodes["orders-db"].name === "Orders Store";
  };
  let ok = false;
  for (let i = 0; i < 40 && !ok; i++) { ok = await apiHas(); if (!ok) await sleep(250); }
  check("API state receives the merged document", ok);
  const activity = await (await fetch(`${API}/api/projects/${project.id}/activity`)).json();
  check("activity is attributed to the editors", activity.some((x: { actor: string }) => /Alice/.test(x.actor) && /Bob/.test(x.actor)),
    JSON.stringify(activity.slice(0, 2)));

  // A server-side edit (what an MCP agent or the AI does) appears in the live document, merged.
  applyOps(b.ydoc, [{ op: "update_node", id: "helpdesk", changes: { description: "edited by Bob" } }]);
  await fetch(`${API}/api/projects/${project.id}/ops`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ actor: "mcp-agent", ops: [{ op: "update_node", id: "helpdesk", changes: { technology: "Freshdesk" } }] }),
  });
  const viaMcp = await waitFor(() => node(a.ydoc, "helpdesk")?.technology === "Freshdesk");
  check("MCP/API edit lands in the live document", viaMcp);
  check("…without overwriting a concurrent human edit", node(a.ydoc, "helpdesk")?.description === "edited by Bob",
    JSON.stringify(node(a.ydoc, "helpdesk")));

  // AI command (offline parser when no LLM key) also flows into the live doc.
  await fetch(`${API}/api/projects/${project.id}/ai/command`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: "Add Redis between Order Service and Orders Store", actor: "Alice" }),
  });
  check("AI command appears on everyone's canvas", await waitFor(() => !!node(b.ydoc, "redis")));

  a.provider.destroy(); b.provider.destroy();
} finally {
  await sleep(300);
  await fetch(`${API}/api/projects/${project.id}`, { method: "DELETE" });
}
console.log(failures ? `\n${failures} check(s) failed` : "\nAll collaboration checks passed");
process.exit(failures ? 1 : 0);
