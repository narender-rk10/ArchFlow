// Dev helper: join a document as another user, add a component, move a cursor, then stay for a while.
import { HocuspocusProvider } from "@hocuspocus/provider";
import * as Y from "yjs";
import { signTicket } from "./ticket.js";
import { applyOps } from "./ydoc.js";
const [doc = "", name = "Priya", seconds = "25"] = process.argv.slice(2);
const ydoc = new Y.Doc();
// Runs next to the collab server, so it mints its own ticket with the shared ARCHFLOW_INTERNAL_TOKEN.
const token = signTicket(process.env.ARCHFLOW_INTERNAL_TOKEN ?? "", { uid: `demo-${name}`, name, color: "#d6409f", doc, ro: false }, Number(seconds) + 60);
const p = new HocuspocusProvider({ url: process.env.COLLAB_WS ?? "ws://localhost:1234", name: doc, document: ydoc, token });
p.on("synced", () => {
  p.awareness!.setLocalStateField("user", { name, color: "#d6409f" });
  applyOps(ydoc, [{ op: "add_node", node: { id: "notification-svc", name: "Notification Service", type: "service", technology: "Node.js", position: { x: 1050, y: 300 } } },
                  { op: "add_edge", edge: { source: "support-agent", target: "notification-svc", protocol: "events" } }]);
  p.awareness!.setLocalStateField("selection", ["notification-svc"]);
  let t = 0;
  const iv = setInterval(() => { t += 1; p.awareness!.setLocalStateField("cursor", { x: 900 + 40 * Math.sin(t / 3), y: 200 + 3 * t }); }, 200);
  setTimeout(() => { clearInterval(iv); p.destroy(); process.exit(0); }, Number(seconds) * 1000);
});
