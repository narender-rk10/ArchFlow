import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const backend = process.env.ARCHFLOW_API ?? "http://localhost:8000";
const collab = process.env.ARCHFLOW_COLLAB ?? "ws://localhost:1234";

export default defineConfig({
  plugins: [react()],
  resolve: {
    // The Yjs document model is shared with the collab server; there must be exactly one copy of yjs.
    alias: { "@collab": fileURLToPath(new URL("../collab/src", import.meta.url)) },
    dedupe: ["yjs"],
  },
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
      "/collab": { target: collab, ws: true, rewrite: (p) => p.replace(/^\/collab/, "") || "/" },
    },
  },
});
